import copy
import json
from unittest.mock import patch

import pytest

from hy3_tracejudge.benchmarking import run_checkpointed, exclusive_run
from hy3_tracejudge.hy3_client import Hy3APIError
from hy3_tracejudge.reporting import write_json


def records():
    return [{"sample_id": f"hy3-{p}", "problem_id": p, "difficulty": "easy", "status": "pending"} for p in ("a", "b")]


def result():
    return {"difficulty": "easy", "final_correct": True, "process_correct": None, "error_types": []}


def run(path, solve, evaluate, **kwargs):
    return run_checkpointed(output=path, manifest=kwargs.pop("manifest", {"settings": {"model": "fake"}}),
                            initial_records=records(), solve=solve, evaluate=evaluate,
                            max_attempts=kwargs.pop("max_attempts", 1), retry_delay=0, **kwargs)


def test_interrupt_after_generation_reuses_answer_and_completed_record(tmp_path):
    output = tmp_path / "benchmark.json"
    calls = []
    def solve(pid):
        calls.append(pid)
        return {"code": pid}, {"request_id": pid}
    def evaluate(pid, answer):
        saved = json.loads(output.read_text(encoding="utf-8"))
        assert saved["records"][0]["answer"]["code"] == "a"
        if pid == "b":
            raise KeyboardInterrupt()
        return result()
    with pytest.raises(KeyboardInterrupt):
        run(output, solve, evaluate)
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["run_status"] == "interrupted"
    assert saved["completed"] == 1
    resumed = run(output, solve, lambda pid, answer: result(), resume=True)
    assert resumed["completed"] == 2
    assert calls == ["a", "b"]
    assert len(output.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_transient_generation_failure_retries_but_auth_error_does_not(tmp_path):
    calls = []
    def solve(pid):
        calls.append(pid)
        if pid == "a" and calls.count(pid) == 1:
            raise Hy3APIError("temporary 429")
        if pid == "b":
            raise Hy3APIError("401", retryable=False)
        return {"code": pid}, {}
    completed = run(tmp_path / "benchmark.json", solve, lambda p, a: result(), max_attempts=3)
    assert calls == ["a", "a", "b"]
    assert completed["completed"] == 1
    assert completed["failed_runs"] == 1
    assert completed["records"][0]["attempt_history"][0]["stage"] == "generation"
    assert completed["records"][1]["failure_owner"] == "infrastructure"
    assert completed["records"][1]["failure_owner_status"] == "provisional"


def test_retry_failed_is_explicit_and_does_not_regenerate_answer(tmp_path):
    output = tmp_path / "benchmark.json"
    calls = []
    def solve(pid):
        calls.append(pid)
        return {"code": pid}, {}
    def broken(pid, answer):
        raise Hy3APIError("temporary reviewer failure")
    run(output, solve, broken)
    unchanged = run(output, solve, broken, resume=True)
    assert unchanged["failed_runs"] == 2
    resumed = run(output, solve, lambda p, a: result(), resume=True, retry_failed=True)
    assert resumed["completed"] == 2
    assert calls == ["a", "b"]
    assert all("run_error" not in r for r in resumed["records"])


def test_partial_review_failure_saved_and_retried_but_semantic_unknown_is_not(tmp_path):
    calls = []
    def evaluate(pid, answer):
        calls.append(pid)
        if pid == "a" and calls.count(pid) == 1:
            return {**result(), "hy3_review": {"status": "error"}}
        return result()
    output = tmp_path / "benchmark.json"
    final = run(output, lambda p: ({"code": p}, {}), evaluate, max_attempts=2)
    assert final["completed"] == 2
    assert calls == ["a", "a", "b"]
    assert "partial_evaluation" not in final["records"][0]


def test_existing_output_and_mismatched_resume_are_rejected_without_calls(tmp_path):
    output = tmp_path / "benchmark.json"
    solve = lambda p: ({"code": p}, {})
    run(output, solve, lambda p, a: result())
    def must_not_call(*args):
        pytest.fail("must reject before calling the model")
    with pytest.raises(FileExistsError):
        run(output, must_not_call, must_not_call)
    with pytest.raises(ValueError, match="不一致"):
        run(output, must_not_call, must_not_call, resume=True, manifest={"settings": {"model": "changed"}})
    data = json.loads(output.read_text(encoding="utf-8"))
    data["records"][0]["answer"]["code"] = "tampered"
    output.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="损坏"):
        run(output, must_not_call, must_not_call, resume=True)


def test_atomic_write_failure_preserves_previous_report(tmp_path):
    output = tmp_path / "report.json"
    write_json(output, {"old": True})
    with patch("hy3_tracejudge.reporting.os.replace", side_effect=OSError("disk error")):
        with pytest.raises(OSError):
            write_json(output, {"new": True})
    assert json.loads(output.read_text()) == {"old": True}
    assert not list(tmp_path.glob("*.tmp"))


def test_same_checkpoint_cannot_have_two_writers(tmp_path):
    with exclusive_run(tmp_path / "report.json"):
        with pytest.raises(RuntimeError, match="另一个进程"):
            with exclusive_run(tmp_path / "report.json"):
                pytest.fail("second writer acquired lock")
