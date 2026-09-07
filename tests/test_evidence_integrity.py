from __future__ import annotations

import copy
import json
from unittest.mock import patch

import pytest

from hy3_tracejudge.catalog import get_problem, load_problems
from hy3_tracejudge.evaluator import evaluate_answer, summarize_results
from hy3_tracejudge.executor import run_cases, ExecutionResult, TestResult as CaseResult
from hy3_tracejudge.fixtures import make_answer, build_labeled_samples
from hy3_tracejudge.hy3_client import Hy3Client, Hy3Config
from hy3_tracejudge.multi_agent import SPECIALISTS, _normalize_review, _build_decision, _merge_arbitration


def positive_reviews(answer):
    return [_normalize_review(spec, {"valid": True, "confidence": 0.95,
        "reason": "已核对题面和实际推导，引用的错误方案已被明确否定。",
        "reviewed_steps": [s["id"] for s in answer["reasoning_steps"] if s["stage"] == spec.stage],
        "first_error_step": None, "error_type": None}) for spec in SPECIALISTS]


def test_negated_keyword_does_not_override_full_semantic_review():
    problem = get_problem("coin_change")
    answer = make_answer(problem, "gold")
    answer["reasoning_steps"][1]["content"] += " 不能采用反复选择当前最大硬币的策略。"
    offline = evaluate_answer(problem, answer, use_hypothesis=False)
    assert offline["final_correct"] is True
    assert offline["process_correct"] is None
    assert all(c["passed"] is None for c in offline["criteria"])
    decision, _ = _build_decision(problem, answer, offline, positive_reviews(answer))
    assert decision["process_correct"] is True
    assert decision["first_error_step"] is None


def test_keyword_stuffing_cannot_establish_process_without_reviewer():
    problem = get_problem("coin_change")
    sample = next(s for s in build_labeled_samples([problem]) if s["profile"] == "keyword_only")
    result = evaluate_answer(problem, sample["answer"], use_hypothesis=False)
    assert result["final_correct"] is True
    assert result["process_correct"] is None
    assert result["unsupported_correct"] is None


def test_reference_case_exception_is_not_a_successful_null_oracle():
    problem = {"function_name": "solve_case", "reference_solution": "def solve_case(case):\n    return 1/0"}
    result = run_cases(problem, "def solve_case(case):\n    return None", [{}])
    assert not result.all_passed
    assert result.harness_error.startswith("ReferenceOracleError")


def test_legitimate_null_reference_result_still_works():
    code = "def solve_case(case):\n    return None"
    result = run_cases({"function_name": "solve_case", "reference_solution": code}, code, [{}])
    assert result.all_passed
    assert result.harness_error is None


def test_incomplete_reference_results_prevent_candidate_execution():
    with patch("hy3_tracejudge.executor.run_candidate", return_value=ExecutionResult(0, 2, False, [])) as run:
        result = run_cases({"reference_solution": "reference"}, "candidate", [{}, {}])
    assert result.harness_error.startswith("ReferenceOracleError")
    assert run.call_count == 1


def test_unassigned_reasoning_step_prevents_process_pass():
    problem = get_problem("coin_change")
    answer = make_answer(problem, "gold")
    answer["reasoning_steps"].append({"id": 6, "stage": "other", "title": "附加结论", "content": "任意结论"})
    evidence = {"execution": {"all_passed": True, "total": 1}}
    result, _ = _build_decision(problem, answer, evidence, positive_reviews(answer))
    assert result["process_correct"] is None


@pytest.mark.parametrize("failed_execution", [False, True])
def test_arbitration_can_retract_semantic_objection_but_not_counterexample(failed_execution):
    problem = get_problem("coin_change")
    answer = make_answer(problem, "gold")
    reviews = positive_reviews(answer)
    reviews[1].update(valid=False, first_error_step=2, error_type="theorem_misuse")
    evidence = {"execution": {"all_passed": not failed_execution, "total": 1}}
    decision, _ = _build_decision(problem, answer, evidence, reviews)
    arbitration = {"process_correct": True, "first_error_step": None, "error_types": [],
                   "reviewed_steps": [1, 2, 3, 4, 5], "confidence": 0.95,
                   "rationale": "逐步复核后撤回对否定贪心示例的误判。"}
    result = _merge_arbitration(decision, arbitration, answer, [])
    assert result["process_correct"] is (False if failed_execution else True)


def test_fixture_samples_are_unique_and_include_adversarial_cases():
    samples = build_labeled_samples(load_problems())
    keys = {(s["problem_id"], json.dumps(s["answer"], sort_keys=True)) for s in samples}
    assert len(keys) == len(samples)
    assert {"keyword_only", "negated_fault"}.issubset({s["profile"] for s in samples})


def test_report_intervals_preserve_uncertain_denominators():
    rows = [{"difficulty": "easy", "final_correct": value, "process_correct": None, "error_types": []}
            for value in [True, False, None]]
    summary = summarize_results(rows)
    low, high = summary["final_accuracy_ci95"]
    assert low < 0.5 < high
    assert summary["final_evaluated_samples"] == 2
    assert summary["process_accuracy_ci95"] is None


def test_health_requires_requested_model():
    client = Hy3Client(Hy3Config(model="hy3"))
    with patch.object(client, "_request", return_value={"data": [{"id": "unrelated"}]}):
        assert client.health()["ok"] is False
