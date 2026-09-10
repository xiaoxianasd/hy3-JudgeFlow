from __future__ import annotations

import copy
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable

from ..catalog import get_problem
from ..evaluator import evaluate_answer
from ..hy3_client import Hy3APIError, Hy3Client, Hy3Config, public_model_error
from ..submissions import SubmissionSandboxRequired


LOGGER = logging.getLogger("hy3_tracejudge.jobs")
TERMINAL_STATUSES = {"succeeded", "failed"}


class JobQueueFull(RuntimeError):
    pass


class JobNotFound(KeyError):
    pass


JobRunner = Callable[..., dict[str, Any]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_evaluation_job(
    problem_id: str,
    hypothesis_examples: int,
    review_mode: str,
    update_phase: Callable[[str], None],
    *,
    submission: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    problem = get_problem(problem_id)
    if submission is not None and runtime is None:
        from ..submissions import evaluate_submission

        return evaluate_submission(
            problem, submission, hypothesis_examples=hypothesis_examples, update_phase=update_phase
        )
    base_config = Hy3Config.from_env()
    client = Hy3Client(replace(
        base_config,
        model=str((runtime or {}).get("model") or base_config.model),
        api_key=str((runtime or {}).get("api_key") or base_config.api_key),
    ))
    if submission is not None:
        from ..submissions import evaluate_submission

        return evaluate_submission(
            problem, submission, hypothesis_examples=hypothesis_examples,
            update_phase=update_phase, hy3_client=client,
        )
    update_phase("hy3_generation")
    answer, generation = client.solve(problem)
    evaluation = evaluate_answer(
        problem,
        answer,
        hypothesis_examples=hypothesis_examples,
        hy3_client=client,
        review_mode=review_mode,
        reveal_hidden=False,
        update_phase=update_phase,
    )
    public_generation = {
        key: generation.get(key)
        for key in ("model", "latency_ms", "request_id", "usage")
        if key in generation
    }
    return {"answer": answer, "generation": public_generation, "evaluation": evaluation}


class EvaluationJobManager:
    """Bounded in-process queue for long Hy3 evaluations.

    A deployment must run one API process. Horizontal scaling requires replacing
    this manager with a shared durable queue and result store.
    """

    def __init__(
        self,
        *,
        workers: int,
        queue_size: int,
        ttl_seconds: int,
        max_stored_jobs: int,
        runner: JobRunner = run_evaluation_job,
    ) -> None:
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="evaluation")
        self._max_active = workers + queue_size
        self._capacity = threading.BoundedSemaphore(self._max_active)
        self._ttl_seconds = ttl_seconds
        self._max_stored_jobs = max_stored_jobs
        self._runner = runner
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._closed = False

    @property
    def accepting_jobs(self) -> bool:
        with self._lock:
            return not self._closed

    def start(self) -> None:
        """Memory backend is ready immediately; kept for manager protocol parity."""

    def submit(
        self, problem_id: str, hypothesis_examples: int, review_mode: str, *,
        submission: dict[str, Any] | None = None,
        runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise JobQueueFull("evaluation service is shutting down")
        if not self._capacity.acquire(blocking=False):
            raise JobQueueFull("evaluation queue is full")
        job_id = uuid.uuid4().hex
        timestamp = _now()
        job = {
            "id": job_id,
            "status": "queued",
            "phase": "queued",
            "problem_id": problem_id,
            "kind": "code_submission" if submission is not None else "hy3_generation",
            "requested_model": (runtime or {}).get("model") or Hy3Config.from_env().model,
            "created_at": timestamp,
            "updated_at": timestamp,
            "finished_monotonic": None,
            "result": None,
            "error": None,
        }
        try:
            with self._lock:
                self._cleanup_locked()
                self._jobs[job_id] = job
            self._executor.submit(
                self._run,
                job_id,
                problem_id,
                hypothesis_examples,
                review_mode,
                copy.deepcopy(submission),
                copy.deepcopy(runtime),
            )
        except BaseException:
            with self._lock:
                self._jobs.pop(job_id, None)
            self._capacity.release()
            raise
        return self.snapshot(job_id)

    def snapshot(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            self._cleanup_locked()
            job = self._jobs.get(job_id)
            if job is None:
                raise JobNotFound(job_id)
            public = {key: value for key, value in job.items() if key != "finished_monotonic"}
            return copy.deepcopy(public)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            self._cleanup_locked()
            counts: dict[str, int] = {}
            for job in self._jobs.values():
                counts[job["status"]] = counts.get(job["status"], 0) + 1
            return {
                "backend": "memory",
                "capacity": self._max_active,
                "counts": counts,
                "active": counts.get("queued", 0) + counts.get("running", 0),
                "oldest_queued_age_seconds": None,
            }

    def _set(self, job_id: str, **values: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.update(values)
            job["updated_at"] = _now()

    def _run(
        self,
        job_id: str,
        problem_id: str,
        hypothesis_examples: int,
        review_mode: str,
        submission: dict[str, Any] | None = None,
        runtime: dict[str, Any] | None = None,
    ) -> None:
        self._set(job_id, status="running", phase="starting")
        update_phase = lambda phase: self._set(job_id, phase=phase)
        try:
            options = {"submission": submission} if submission is not None else {}
            if runtime is not None:
                options["runtime"] = runtime
            result = self._runner(problem_id, hypothesis_examples, review_mode, update_phase, **options)
            self._set(
                job_id,
                status="succeeded",
                phase="completed",
                result=result,
                finished_monotonic=time.monotonic(),
            )
        except SubmissionSandboxRequired:
            self._set(
                job_id, status="failed", phase="failed",
                error={"code": "submission_sandbox_required", "message": "用户代码任务需要 Docker 安全沙盒，请检查配置后重新提交",
                       "failure_owner": "infrastructure", "failure_owner_status": "provisional"},
                finished_monotonic=time.monotonic(),
            )
        except Hy3APIError as exc:
            LOGGER.exception("Model evaluation job %s failed", job_id)
            self._set(
                job_id,
                status="failed",
                phase="failed",
                error=public_model_error(exc, str((runtime or {}).get("model") or Hy3Config.from_env().model)),
                finished_monotonic=time.monotonic(),
            )
        except Exception:
            LOGGER.exception("Evaluation job %s failed", job_id)
            self._set(
                job_id,
                status="failed",
                phase="failed",
                error={"code": "evaluation_failed", "message": "评估执行失败，请联系管理员并提供任务编号",
                       "failure_owner": "evaluator", "failure_owner_status": "provisional"},
                finished_monotonic=time.monotonic(),
            )
        finally:
            self._capacity.release()

    def _cleanup_locked(self) -> None:
        cutoff = time.monotonic() - self._ttl_seconds
        expired = [
            job_id
            for job_id, job in self._jobs.items()
            if job["status"] in TERMINAL_STATUSES
            and isinstance(job["finished_monotonic"], float)
            and job["finished_monotonic"] < cutoff
        ]
        for job_id in expired:
            self._jobs.pop(job_id, None)
        overflow = len(self._jobs) - self._max_stored_jobs
        if overflow > 0:
            removable = [
                job_id for job_id, job in self._jobs.items() if job["status"] in TERMINAL_STATUSES
            ]
            for job_id in removable[:overflow]:
                self._jobs.pop(job_id, None)

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
