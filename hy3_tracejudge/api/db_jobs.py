from __future__ import annotations

import copy
import logging
import threading
import time
import uuid
from typing import Any

from ..hy3_client import Hy3APIError, Hy3Config, public_model_error
from ..submissions import SubmissionSandboxRequired
from .database import DatabaseUnavailable, MySQLJobStore
from .jobs import JobRunner, run_evaluation_job


LOGGER = logging.getLogger("hy3_tracejudge.db_jobs")


class DatabaseEvaluationJobManager:
    """Durable lease-based worker queue backed by MySQL."""

    def __init__(
        self,
        store: MySQLJobStore,
        *,
        workers: int,
        poll_interval_seconds: float,
        lease_seconds: int,
        heartbeat_seconds: int,
        retention_seconds: int,
        runner: JobRunner = run_evaluation_job,
    ) -> None:
        self._store = store
        self._workers = workers
        self._poll_interval = poll_interval_seconds
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._retention_seconds = retention_seconds
        self._runner = runner
        self._instance_id = uuid.uuid4().hex[:12]
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._runtime_lock = threading.Lock()
        self._runtime_by_job: dict[str, dict[str, Any]] = {}
        self._started = False
        self._closed = False

    @property
    def accepting_jobs(self) -> bool:
        with self._lock:
            active = self._started and not self._closed
        return active and self._store.ping()

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            if self._closed:
                raise RuntimeError("database job manager is closed")
            self._started = True
            for index in range(self._workers):
                thread = threading.Thread(
                    target=self._worker_loop,
                    args=(index,),
                    name=f"db-evaluation-{index}",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()

    def submit(
        self, problem_id: str, hypothesis_examples: int, review_mode: str, *,
        submission: dict[str, Any] | None = None,
        runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                from .jobs import JobQueueFull

                raise JobQueueFull("evaluation service is shutting down")
        requested_model = str((runtime or {}).get("model") or Hy3Config.from_env().model)
        requires_credentials = bool((runtime or {}).get("api_key"))
        # Hold this lock across the database commit and registry write. A worker
        # may claim the committed row immediately, but cannot read credentials
        # until the in-memory entry is present.
        with self._runtime_lock:
            job = self._store.enqueue(
                problem_id, hypothesis_examples, review_mode,
                submission=submission,
                requested_model=requested_model,
                requires_transient_credentials=requires_credentials,
            )
            if runtime is not None:
                self._runtime_by_job[job["id"]] = copy.deepcopy(runtime)
        return job

    def _discard_runtime(self, job_id: str) -> None:
        with self._runtime_lock:
            self._runtime_by_job.pop(job_id, None)

    def snapshot(self, job_id: str) -> dict[str, Any]:
        return self._store.get(job_id)

    def stats(self) -> dict[str, Any]:
        return self._store.stats() | {
            "worker_threads": self._workers,
            "instance_id": self._instance_id,
        }

    def _worker_loop(self, worker_index: int) -> None:
        worker_id = f"{self._instance_id}-{worker_index}"
        last_maintenance = 0.0
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                if now - last_maintenance >= 30:
                    recovered, failed = self._store.recover_expired_leases()
                    purged = self._store.purge_finished(self._retention_seconds)
                    if recovered or failed or purged:
                        LOGGER.info(
                            "Queue maintenance recovered=%s failed=%s purged=%s",
                            recovered,
                            failed,
                            purged,
                        )
                    last_maintenance = now
                job = self._store.claim(worker_id, self._lease_seconds)
                if job is None:
                    self._stop.wait(self._poll_interval)
                    continue
                self._execute(worker_id, job)
            except DatabaseUnavailable:
                LOGGER.exception("MySQL queue worker %s is unavailable", worker_id)
                self._stop.wait(min(5.0, max(1.0, self._poll_interval * 4)))
            except Exception:
                LOGGER.exception("Unexpected queue worker error: %s", worker_id)
                self._stop.wait(1.0)

    def _execute(self, worker_id: str, job: dict[str, Any]) -> None:
        job_id = job["id"]
        heartbeat_stop = threading.Event()

        def heartbeat_loop() -> None:
            while not heartbeat_stop.wait(self._heartbeat_seconds):
                try:
                    if not self._store.heartbeat(job_id, worker_id, self._lease_seconds):
                        LOGGER.error("Lease lost for job %s", job_id)
                        return
                except DatabaseUnavailable:
                    LOGGER.exception("Heartbeat failed for job %s", job_id)

        heartbeat = threading.Thread(
            target=heartbeat_loop,
            name=f"job-heartbeat-{job_id[:8]}",
            daemon=True,
        )
        heartbeat.start()

        def update_phase(phase: str) -> None:
            self._store.set_phase(job_id, worker_id, phase)

        try:
            options = {"submission": job["submission"]} if job.get("submission") is not None else {}
            with self._runtime_lock:
                runtime = copy.deepcopy(self._runtime_by_job.get(job_id))
            if job.get("requires_transient_credentials") and not (runtime or {}).get("api_key"):
                self._store.fail_or_retry(
                    job_id, worker_id,
                    {"code": "transient_credentials_lost",
                     "message": "浏览器提供的模型 API Key 已因服务重启失效，请重新提交任务",
                     "failure_owner": "infrastructure", "failure_owner_status": "provisional"},
                    retryable=False,
                )
                self._discard_runtime(job_id)
                return
            if runtime is None and job.get("requested_model") != Hy3Config.from_env().model:
                runtime = {"model": job["requested_model"], "api_key": None}
            if runtime is not None:
                options["runtime"] = runtime
            result = self._runner(
                job["problem_id"],
                int(job["hypothesis_examples"]),
                str(job["review_mode"]),
                update_phase,
                **options,
            )
            if not self._store.complete(job_id, worker_id, result):
                LOGGER.error("Completion ignored because lease was lost for job %s", job_id)
            else:
                self._discard_runtime(job_id)
        except SubmissionSandboxRequired:
            self._store.fail_or_retry(
                job_id, worker_id,
                {"code": "submission_sandbox_required", "message": "用户代码任务需要 Docker 安全沙盒，请检查配置后重新提交",
                 "failure_owner": "infrastructure", "failure_owner_status": "provisional"},
                retryable=False,
            )
            self._discard_runtime(job_id)
        except Hy3APIError as exc:
            LOGGER.exception("Model evaluation job %s failed", job_id)
            error = public_model_error(
                exc,
                str(job.get("requested_model") or Hy3Config.from_env().model),
                attempts=int(job.get("attempts") or 1),
                max_attempts=int(job.get("max_attempts") or 1),
            )
            outcome = self._store.fail_or_retry(
                job_id,
                worker_id,
                error,
                retryable=exc.retryable,
                retry_after_seconds=exc.retry_after_seconds,
            )
            if outcome != "retrying":
                self._discard_runtime(job_id)
        except Exception:
            LOGGER.exception("Evaluation job %s failed", job_id)
            self._store.fail_or_retry(
                job_id,
                worker_id,
                {
                    "code": "evaluation_failed",
                    "message": "评估执行失败，请联系管理员并提供任务编号",
                    "failure_owner": "evaluator",
                    "failure_owner_status": "provisional",
                },
                retryable=False,
            )
            self._discard_runtime(job_id)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=1)

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)
        with self._runtime_lock:
            self._runtime_by_job.clear()
        self._store.engine.dispose()
