from __future__ import annotations

import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hypothesis import given, settings, strategies as st

from hy3_tracejudge.catalog import get_problem
from hy3_tracejudge.evaluator import evaluate_answer, summarize_results, validate_evaluator
from hy3_tracejudge.executor import ExecutionResult, TestResult as CaseResult
from hy3_tracejudge.fixtures import make_answer
from hy3_tracejudge.hy3_client import Hy3APIError
from hy3_tracejudge.multi_agent import SPECIALISTS, _build_decision, _merge_arbitration, _normalize_review
from hy3_tracejudge.property_testing import find_counterexample
from hy3_tracejudge.reporting import write_results_csv
from hy3_tracejudge.verdicts import confidence_value, test_verdict as executable_verdict


PROBLEM = get_problem("mbpp_Mbpp/77")
ANSWER = make_answer(get_problem("two_sum_exists"), "gold")
PASS = ExecutionResult(1, 1, True, [CaseResult("public", True, True, True, visibility="public")])
EVIDENCE = {"criteria": [], "execution": PASS.to_dict(), "hypothesis": {"enabled": False, "found": False}}


def reviews_for(values):
    return [_normalize_review(spec, {
        "valid": value, "reviewed_steps": [index], "first_error_step": index if value is False else None,
        "error_type": "algorithm_error" if value is False else None,
        "confidence": 0.9, "reason": "Deterministic test evidence", "evidence": ["test evidence"],
    }) for index, (spec, value) in enumerate(zip(SPECIALISTS, values), 1)]


class ReviewClient:
    def __init__(self, *, fail=False, valid=True, confidence=0.9, location=None, covered=True):
        self.fail, self.valid, self.confidence = fail, valid, confidence
        self.location, self.covered = location, covered

    def review_stage(self, problem, answer, evidence, *, agent_name, stage, responsibility):
        if self.fail:
            raise Hy3APIError("simulated unavailable reviewer")
        return {"valid": self.valid, "confidence": self.confidence, "reason": "Test review evidence",
                "reviewed_steps": [s["id"] for s in answer["reasoning_steps"] if s["stage"] == stage] if self.covered else [],
                "first_error_step": self.location, "error_type": "algorithm_error" if self.valid is False else None}, {}

    def review(self, problem, answer, evidence):
        if self.fail:
            raise Hy3APIError("simulated unavailable reviewer")
        return {"process_correct": self.valid, "confidence": self.confidence, "rationale": "Test review evidence",
                "first_error_step": self.location, "error_types": ["algorithm_error"] if self.valid is False else [],
                "step_reviews": [{"step": s["id"], "valid": self.valid} for s in answer["reasoning_steps"]] if self.covered else []}, {}

    def arbitrate_reviews(self, *args):
        return {"process_correct": True, "first_error_step": None, "confidence": 0.95, "rationale": "test arbitration"}, {}


class VerdictTests(unittest.TestCase):
    def evaluate(self, client=None, mode="supervisor", execution=None, property_result=None):
        with patch("hy3_tracejudge.evaluator.run_candidate", return_value=execution or copy.deepcopy(PASS)), \
             patch("hy3_tracejudge.evaluator.find_counterexample", return_value=property_result or {"enabled": False, "found": False}):
            return evaluate_answer(PROBLEM, ANSWER, hy3_client=client, review_mode=mode)

    def test_complete_positive_review_passes_in_both_modes(self):
        for mode in ("single", "supervisor"):
            with self.subTest(mode=mode):
                result = self.evaluate(ReviewClient(), mode)
                self.assertIs(result["process_correct"], True)
                self.assertEqual(result["process_status"], "valid")
                self.assertEqual(result["review_coverage"]["completed"], 1 if mode == "single" else 5)

    def test_no_review_or_empty_rubric_cannot_establish_process(self):
        result = self.evaluate()
        self.assertIs(result["final_correct"], True)
        self.assertIsNone(result["process_correct"])

    def test_unavailable_low_confidence_missing_coverage_and_null_abstain(self):
        for mode in ("single", "supervisor"):
            for client in (ReviewClient(fail=True), ReviewClient(confidence=0.2), ReviewClient(valid=False, confidence=0.2),
                           ReviewClient(valid=None), ReviewClient(valid="false"), ReviewClient(covered=False),
                           ReviewClient(confidence=float("nan")), ReviewClient(confidence=2.0)):
                with self.subTest(mode=mode, client=vars(client)):
                    result = self.evaluate(client, mode)
                    self.assertIsNone(result["process_correct"])
                    self.assertIsNone(result["unsupported_correct"])
                    self.assertIsNone(result["first_error_step"])
                    self.assertEqual(result["process_status"], "uncertain")
                    self.assertEqual(result["error_types"], [])
                    json.dumps(result, allow_nan=False)

    def test_one_missing_reviewer_prevents_positive_conclusion(self):
        reviews = reviews_for([True] * 5)
        result, _ = _build_decision(PROBLEM, ANSWER, EVIDENCE, reviews[:-1])
        self.assertIsNone(result["process_correct"])
        self.assertEqual(result["review_coverage"], {"expected": 5, "completed": 4, "conclusive": 4})

    def test_confirmed_error_does_not_require_a_valid_location(self):
        for mode in ("single", "supervisor"):
            for location in (None, 0, -1, 99, True, "2", 2.0):
                with self.subTest(mode=mode, location=location):
                    result = self.evaluate(ReviewClient(valid=False, location=location), mode)
                    self.assertIs(result["process_correct"], False)
                    self.assertIs(result["unsupported_correct"], True)
                    self.assertIsNone(result["first_error_step"])
                    self.assertEqual(result["localization_status"], "unlocalized")

    def test_confirmed_error_has_valid_location(self):
        result = self.evaluate(ReviewClient(valid=False, location=2))
        self.assertIs(result["process_correct"], False)
        self.assertEqual(result["first_error_step"], 2)

    def test_execution_failure_survives_reviewer_outage(self):
        result = self.evaluate(ReviewClient(fail=True), execution=ExecutionResult(0, 1, False, []))
        self.assertIs(result["final_correct"], False)
        self.assertIs(result["process_correct"], False)
        self.assertIn("implementation_error", result["error_types"])

    def test_broken_verifier_and_zero_checks_are_not_algorithm_failure_or_success(self):
        for execution in (ExecutionResult(0, 1, False, [], "SandboxBackendUnavailable:docker"), ExecutionResult(0, 0, True, [])):
            result = self.evaluate(ReviewClient(), execution=execution)
            self.assertIsNone(result["final_correct"])
            self.assertIsNone(result["process_correct"])
            self.assertNotIn("implementation_error", result["error_types"])
        result = self.evaluate(ReviewClient(), property_result={"enabled": True, "found": False, "status": "error", "error": "oracle unavailable"})
        self.assertIsNone(result["final_correct"])
        self.assertIsNone(result["process_correct"])

    def test_swarm_null_arbitration_location_does_not_crash_or_erase_error(self):
        result = self.evaluate(ReviewClient(valid=False, location=2), "swarm")
        self.assertIs(result["process_correct"], False)
        self.assertEqual(result["first_error_step"], 2)

    def test_swarm_cannot_pass_after_specialist_outage(self):
        result = self.evaluate(ReviewClient(fail=True), "swarm")
        self.assertIsNone(result["process_correct"])

    def test_inconclusive_arbitration_cannot_finalize_a_provisional_pass(self):
        decision, _ = _build_decision(PROBLEM, ANSWER, EVIDENCE, reviews_for([True] * 5))
        for raw in ({"process_correct": None, "confidence": 0.9},
                    {"process_correct": True, "confidence": 0.1, "rationale": "weak"}):
            result = _merge_arbitration(decision, raw, ANSWER, [])
            self.assertIsNone(result["process_correct"])

    def test_boolean_or_duplicate_answer_ids_are_format_errors(self):
        for value in (True, "1", 2):
            answer = copy.deepcopy(ANSWER)
            answer["reasoning_steps"][0]["id"] = value
            with patch("hy3_tracejudge.evaluator.run_candidate") as execute:
                result = evaluate_answer(PROBLEM, answer)
                self.assertEqual(result["error_types"], ["format_error"])
                self.assertIsNone(result["first_error_step"])
                execute.assert_not_called()

    def test_shape_failure_is_not_fabricated_step_one(self):
        result = evaluate_answer(PROBLEM, {})
        self.assertIs(result["process_correct"], False)
        self.assertIsNone(result["first_error_step"])
        self.assertIsNone(result["final_correct"])

    @settings(max_examples=80, deadline=None, database=None)
    @given(st.lists(st.sampled_from([True, False, None]), min_size=5, max_size=5))
    def test_evidence_reduction_obeys_three_valued_logic_and_order_independence(self, values):
        reviews = reviews_for(values)
        decision, _ = _build_decision(PROBLEM, ANSWER, EVIDENCE, copy.deepcopy(reviews))
        reversed_decision, _ = _build_decision(PROBLEM, ANSWER, EVIDENCE, list(reversed(copy.deepcopy(reviews))))
        expected = False if any(value is False for value in values) else None if any(value is None for value in values) else True
        self.assertIs(decision["process_correct"], expected)
        self.assertEqual(decision, reversed_decision)

    def test_bad_confidence_is_not_silently_promoted_to_certainty(self):
        for value in (True, "invalid", float("nan"), float("inf"), 2, -1):
            self.assertEqual(confidence_value(value), 0)


class PropertyStatusTests(unittest.TestCase):
    @patch("hy3_tracejudge.property_testing.run_cases")
    def test_unsupported_problem_never_executes_even_with_large_budget(self, run):
        result = find_counterexample({**PROBLEM, "id": "unregistered_future_problem"}, "must not run", max_examples=500)
        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(result["examples_checked"], 0)
        run.assert_not_called()

    @patch("hy3_tracejudge.property_testing.run_cases", return_value=ExecutionResult(0, 1, False, [], "ReferenceOracleError: private diagnostic"))
    def test_oracle_failure_is_not_a_counterexample(self, run):
        result = find_counterexample(get_problem("two_sum_exists"), "not executed", max_examples=1)
        self.assertEqual(result["status"], "error")
        self.assertIs(result["found"], False)
        self.assertIsNone(result["counterexample"])
        self.assertNotIn("private diagnostic", json.dumps(result))

    def test_real_fixed_failure_remains_false_if_property_service_fails(self):
        self.assertIs(executable_verdict({"all_passed": False, "total": 1}, {"enabled": True, "error": "unavailable"}), False)

    def test_incomplete_property_result_is_not_a_pass(self):
        for result in ({"enabled": True}, {"enabled": True, "found": None},
                       {"enabled": True, "status": "error", "found": False}, {"found": False}):
            self.assertIsNone(executable_verdict({"all_passed": True, "total": 1}, result))


class ReportingVerdictTests(unittest.TestCase):
    def test_summary_separates_unknown_and_reports_denominators(self):
        records = [{"difficulty": "hard", "final_correct": True, "process_correct": value,
                    "error_types": ["algorithm_error"] if value is False else []} for value in (True, False, None)]
        result = summarize_results(records)
        self.assertEqual(result["process_accuracy"], 0.5)
        self.assertEqual(result["process_uncertain_count"], 1)
        self.assertEqual(result["process_evaluated_samples"], 2)
        self.assertEqual(result["process_coverage"], 2/3)
        self.assertEqual(result["unsupported_correct_count"], 1)
        self.assertEqual(result["by_difficulty"]["hard"]["process_uncertain_count"], 1)
        self.assertIsNone(summarize_results(records[2:])["process_accuracy"])
        self.assertIsNone(summarize_results([])["process_accuracy"])

    def test_abstention_is_not_detection_false_positive_or_null_location_match(self):
        rows = [{"ground_truth": {"final_correct": final, "process_valid": final, "first_error_step": None},
                 "evaluation": {"process_correct": None, "first_error_step": None}} for final in (True, False)]
        result = validate_evaluator(rows)
        self.assertEqual(result["process_problem_detection_rate"], 0)
        self.assertEqual(result["exact_step_localization_accuracy"], 0)
        self.assertEqual(result["false_positive_count"], 0)
        self.assertEqual(result["flagged_answer_correct_samples"], 0)
        self.assertIsNone(result["false_positive_rate_on_sound_processes"])
        self.assertEqual(result["process_uncertain_samples"], 2)

    def test_csv_keeps_unknown_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.csv"
            write_results_csv(path, [{"process_correct": None, "final_correct": True}])
            with path.open(encoding="utf-8-sig", newline="") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual(row["process_status"], "uncertain")
            self.assertEqual(row["process_correct"], "")
