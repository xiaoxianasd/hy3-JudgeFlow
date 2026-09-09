"""Durable sequential model benchmarks; a checkpoint is also a readable report."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .evaluator import summarize_results
from .hy3_client import Hy3APIError
from .reporting import write_json, write_jsonl, write_results_csv


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def implementation_fingerprint() -> str:
    root = Path(__file__).parent
    return fingerprint({str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in sorted(root.rglob("*.py"))})


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def exclusive_run(output: Path):
    """OS releases the lock after a crash; the reusable lock file is harmless."""
    output.parent.mkdir(parents=True, exist_ok=True)
    stream = output.with_name(output.name + ".lock").open("a+b")
    locked = False
    try:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise RuntimeError("该评测文件正在被另一个进程使用") from exc
        yield
    finally:
        if locked:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


def unavailable_evaluation(evaluation: dict[str, Any]) -> bool:
    """Retry unavailable infrastructure/review calls, not genuine uncertainty."""
    execution = evaluation.get("execution") or {}
    hypothesis = evaluation.get("hypothesis") or {}
    review = evaluation.get("hy3_review") or {}
    orchestration = evaluation.get("orchestration") or {}
    from .verdicts import is_infrastructure_error
    return (is_infrastructure_error(execution.get("harness_error"))
            or hypothesis.get("status") == "error"
            or review.get("status") == "error"
            or any(r.get("status") == "error" for r in orchestration.get("specialist_reviews", []))
            or any(c.get("kind") == "arbitration_unavailable" for c in orchestration.get("conflicts", [])))


def run_checkpointed(
    *, output: Path, manifest: dict[str, Any], initial_records: list[dict[str, Any]],
    solve: Callable, evaluate: Callable, resume: bool = False, retry_failed: bool = False,
    max_attempts: int = 2, retry_delay: float = 1.0,
) -> dict[str, Any]:
    if max_attempts < 1 or retry_delay < 0:
        raise ValueError("重试次数必须为正，等待时间不得为负")
    with exclusive_run(output):
        if resume:
            saved = json.loads(output.read_text(encoding="utf-8"))
            if saved.get("checkpoint_version") != 1 or saved.get("manifest") != manifest:
                raise ValueError("断点与当前题目、模型、评测参数或代码不一致，请使用新的输出文件")
            if [r.get("sample_id") for r in saved["records"]] != [r["sample_id"] for r in initial_records]:
                raise ValueError("断点样本列表不完整或顺序不一致")
            records = saved["records"]
            for r in records:
                if r.get("record_sha256") != fingerprint({k: v for k, v in r.items() if k != "record_sha256"}):
                    raise ValueError("断点记录已改变或损坏，拒绝继续运行")
        else:
            if output.exists() or output.with_suffix(".jsonl").exists() or output.with_suffix(".csv").exists():
                raise FileExistsError("输出已存在；续跑请使用 --resume，否则选择新文件名")
            records = initial_records
            saved = {"checkpoint_version": 1, "manifest": manifest, "created_at": now(),
                     "run_type": "hy3_model_benchmark", **manifest["settings"], "records": records}

        def persist(status="running"):
            for r in records:
                r["record_sha256"] = fingerprint({k: v for k, v in r.items() if k != "record_sha256"})
            completed = [r["evaluation"] for r in records if r.get("status") == "completed"]
            saved.update(run_status=status, updated_at=now(), summary=summarize_results(completed),
                         completed=len(completed), failed_runs=sum(r.get("status") == "failed" for r in records),
                         pending_runs=sum(r.get("status") not in {"completed", "failed"} for r in records))
            write_json(output, saved)

        persist()
        try:
            for index, record in enumerate(records, 1):
                if record.get("status") == "completed" or (record.get("status") == "failed" and not retry_failed):
                    continue
                record.pop("run_error", None)
                record.pop("failure_owner", None)
                record.pop("failure_owner_status", None)
                for attempt in range(max_attempts):
                    record["attempts"] = record.get("attempts", 0) + 1
                    stage = "generation" if "answer" not in record else "evaluation"
                    record["status"] = "generating" if stage == "generation" else "evaluating"
                    persist()
                    print(f"[{index}/{len(records)}] {record['problem_id']} · {stage} · attempt {record['attempts']}",
                          file=sys.stderr, flush=True)
                    try:
                        if "answer" not in record:
                            answer, generation = solve(record["problem_id"])
                            record.update(answer=answer, generation_metadata=generation, status="generated")
                            persist()
                        stage = "evaluation"
                        record["status"] = "evaluating"
                        persist()
                        result = evaluate(record["problem_id"], record["answer"])
                        if unavailable_evaluation(result):
                            record["partial_evaluation"] = result
                            raise Hy3APIError(
                                "执行或评审服务暂不可用，保留答案和部分证据以供重试",
                                failure_owner=result.get("failure_owner") or "infrastructure",
                            )
                    except Hy3APIError as exc:
                        record.setdefault("attempt_history", []).append(
                            {"attempt": record["attempts"], "stage": stage, "error": str(exc), "at": now(),
                             "retryable": getattr(exc, "retryable", True),
                             "failure_owner": exc.failure_owner})
                        record.update(status="failed", run_error=str(exc),
                                      failure_owner=exc.failure_owner,
                                      failure_owner_status="provisional")
                        persist()
                        if not getattr(exc, "retryable", True) or attempt + 1 == max_attempts:
                            break
                        time.sleep(min(30.0, retry_delay * 2 ** min(attempt, 5)))
                    else:
                        record.update(evaluation=result, status="completed")
                        record.pop("partial_evaluation", None)
                        record.pop("run_error", None)
                        record.pop("failure_owner", None)
                        record.pop("failure_owner_status", None)
                        persist()
                        break
        except BaseException:
            persist("interrupted")
            raise
        persist("completed_with_failures" if any(r.get("status") == "failed" for r in records) else "completed")
        write_jsonl(output.with_suffix(".jsonl"), records)
        write_results_csv(output.with_suffix(".csv"), records)
        return saved
