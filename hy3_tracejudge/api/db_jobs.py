from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

from ..hy3_client import Hy3APIError
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

    def submit(self, problem_id: str, hypothesis_examples: int, review_mode: str) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                from .jobs import JobQueueFull

                raise JobQueueFull("evaluation service is shutting down")
        return self._store.enqueue(problem_id, hypothesis_examples, review_mode)

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
            result = self._runner(
                job["problem_id"],
                int(job["hypothesis_examples"]),
                str(job["review_mode"]),
                update_phase,
            )
            if not self._store.complete(job_id, worker_id, result):
                LOGGER.error("Completion ignored because lease was lost for job %s", job_id)
        except Hy3APIError:
            LOGGER.exception("Hy3 evaluation job %s failed", job_id)
            self._store.fail_or_retry(
                job_id,
                worker_id,
                {"code": "upstream_error", "message": "Hy3 服务调用失败，请稍后重试"},
                retryable=True,
            )
        except Exception:
            LOGGER.exception("Evaluation job %s failed", job_id)
            self._store.fail_or_retry(
                job_id,
                worker_id,
                {
                    "code": "evaluation_failed",
                    "message": "评估执行失败，请联系管理员并提供任务编号",
                },
                retryable=False,
            )
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
        self._store.engine.dispose()
