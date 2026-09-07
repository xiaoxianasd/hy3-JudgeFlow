import copy
import json

import pytest

from hy3_tracejudge.auditing import build_audit_queue, summarize_audits
from hy3_tracejudge.cli import build_parser, command_audit_export, _select_benchmark_problems
from hy3_tracejudge.catalog import load_problems


def benchmark():
    records = []
    for i, (final, process, step) in enumerate([(False, False, 2), (True, False, 2), (True, False, 1), (True, None, None)]):
        records.append({"sample_id": str(i), "problem_id": "example", "problem": {"statement": "test problem"},
                        "answer": {"code": "pass", "reasoning_steps": [{"id": 1}, {"id": 2}]},
                        "evaluation": {"final_correct": final, "process_correct": process, "first_error_step": step}})
    return {"records": records}


def completed_audits(data):
    rows = build_audit_queue(data)
    for row, (final, valid, step) in zip(rows, [(False, False, 2), (True, False, 2), (True, True, None)]):
        row.update(status="completed", independent_human_audit=True, annotator="test-reviewer",
                   final_answer_correct=final, human_process_valid=valid, first_error_step=step,
                   evidence="Synthetic test annotation, not an actual human audit.")
    return rows


def test_blind_export_and_pending_summary():
    data = benchmark()
    rows = build_audit_queue(data)
    assert all("evaluation" not in row and "ground_truth" not in row for row in rows)
    result = summarize_audits(data, rows)
    assert result["reviewed_samples"] == 0
    assert result["metrics"]["exact_step_localization_accuracy"] is None
    assert result["metrics"]["flagged_false_positive_ratio"] is None


def test_summary_reports_localization_false_positive_ratio_and_coverage():
    data = benchmark()
    result = summarize_audits(data, completed_audits(data))
    assert result["audit_coverage"] == 0.75
    assert result["flagged_audit_coverage"] == 1
    assert result["metrics"]["exact_step_localization_accuracy"] == 1
    assert result["metrics"]["flagged_real_issue_ratio"] == 0.5
    assert result["metrics"]["flagged_false_positive_ratio"] == 0.5


@pytest.mark.parametrize("change", ["hash", "duplicate", "independence", "step", "bool", "evidence"])
def test_rejects_mixed_runs_and_invalid_annotations(change):
    data = benchmark()
    rows = completed_audits(data)
    if change == "hash":
        data["records"][0]["answer"]["code"] = "changed"
    elif change == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    elif change == "independence":
        rows[0]["independent_human_audit"] = False
    elif change == "step":
        rows[0]["first_error_step"] = True
    elif change == "bool":
        rows[0]["human_process_valid"] = "false"
    else:
        rows[0]["evidence"] = ""
    with pytest.raises(ValueError):
        summarize_audits(data, rows)


def test_export_never_overwrites_existing_human_work(tmp_path):
    source = tmp_path / "benchmark.json"
    source.write_text(json.dumps(benchmark()), encoding="utf-8")
    target = tmp_path / "human.jsonl"
    target.write_text("already reviewed", encoding="utf-8")
    args = build_parser().parse_args(["audit-export", "--benchmark", str(source), "--output", str(target)])
    with pytest.raises(FileExistsError):
        command_audit_export(args)
    assert target.read_text(encoding="utf-8") == "already reviewed"


def test_balanced_selection_is_deterministic_and_rejects_insufficient_tier():
    parser = build_parser()
    args = parser.parse_args(["benchmark", "--source", "hy3", "--tier", "seed", "--per-difficulty", "2"])
    selected = _select_benchmark_problems(load_problems(), args)
    assert [p["difficulty"] for p in selected] == ["easy", "medium", "hard"] * 2
    args.per_difficulty = 3
    with pytest.raises(ValueError):
        _select_benchmark_problems(load_problems(), args)
