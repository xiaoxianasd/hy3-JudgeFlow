import copy

import pytest

from hy3_tracejudge.merging import merge_fixture_reports


def record(verdict, step=None):
    return {"sample_id": "s", "sample_origin": "controlled", "difficulty": "easy",
            "answer": {"code": "pass"},
            "ground_truth": {"final_correct": True, "process_valid": False, "first_error_step": 3},
            "evaluation": {"final_correct": True, "process_correct": verdict,
                           "first_error_step": step, "error_types": []}}


def report(item):
    return {"run_type": "evaluator_validation_fixtures", "records": [item]}


def test_merge_prefers_conclusive_retry_without_double_counting():
    result = merge_fixture_reports([("first", report(record(None))),
                                    ("retry", report(record(False, 3)))])
    assert len(result["records"]) == 1
    assert result["process_detection"]["by_origin"]["controlled"]["all_processes"]["detected"] == 1
    assert result["records"][0]["selected_attempt_source"] == "retry"


def test_merge_rejects_conflicting_conclusive_retries():
    with pytest.raises(ValueError):
        merge_fixture_reports([("a", report(record(False, 3))),
                               ("b", report(record(True)))])


def test_merge_rejects_changed_gold_or_answer():
    changed = copy.deepcopy(record(None))
    changed["answer"]["code"] = "changed"
    with pytest.raises(ValueError):
        merge_fixture_reports([("a", report(record(None))), ("b", report(changed))])
