from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from hy3_tracejudge.api.jobs import EvaluationJobManager, run_evaluation_job
from hy3_tracejudge.catalog import get_problem
from hy3_tracejudge.executor import ExecutionResult, TestResult as CaseResult
from hy3_tracejudge.hy3_client import Hy3APIError
from hy3_tracejudge.submissions import SubmissionSandboxRequired, evaluate_submission, normalize_review, require_submission_sandbox


CODE = "def solve_case(case):\n    return True\n"
REVIEW = {"code_correct": True, "process_correct": True, "confidence": 0.9, "reason": "基于给定证据的测试审查", "findings": []}


class SubmissionTests(unittest.TestCase):
    def run_case(self, *, steps=None, review=None, passed=True, harness=None, property_result=None):
        execution = ExecutionResult(int(passed), 1, passed, [CaseResult("hidden", True, passed, passed, error="secret-input" if not passed else None)], harness)
        with patch.dict("os.environ", {"SANDBOX_BACKEND": "docker"}), \
             patch("hy3_tracejudge.submissions.run_candidate", return_value=execution), \
             patch("hy3_tracejudge.submissions.find_counterexample", return_value=property_result or {"enabled": True, "found": False}), \
             patch("hy3_tracejudge.submissions.Hy3Client") as client:
            if isinstance(review, Exception):
                client.return_value.review_submission.side_effect = review
            else:
                client.return_value.review_submission.return_value = (review or REVIEW, {"model": "mock"})
            return evaluate_submission(get_problem("two_sum_exists"), {"code": CODE, "reasoning_steps": steps or []}, hypothesis_examples=2, update_phase=lambda _: None)

    def test_code_only_does_not_invent_or_fail_reasoning(self):
        result = self.run_case()
        self.assertEqual(result["source"], "user_submission")
        self.assertEqual(result["answer"]["code"], CODE)
        self.assertEqual(result["answer"]["reasoning_steps"], [])
        evaluation = result["evaluation"]
        self.assertTrue(evaluation["final_correct"])
        self.assertTrue(evaluation["code_correct"])
        self.assertIsNone(evaluation["process_correct"])
        self.assertIsNone(evaluation["first_error_step"])
        self.assertIsNone(evaluation["unsupported_correct"])

    def test_correct_tests_can_still_have_wrong_submitted_reasoning(self):
        review = {**REVIEW, "process_correct": False, "findings": [{"scope": "reasoning", "step": 2, "error_type": "unjustified_jump", "reason": "第二步没有推出结论"}]}
        evaluation = self.run_case(steps=["输入说明", "随意猜测结论"], review=review)["evaluation"]
        self.assertTrue(evaluation["final_correct"])
        self.assertFalse(evaluation["process_correct"])
        self.assertTrue(evaluation["unsupported_correct"])
        self.assertEqual(evaluation["first_error_step"], 2)
        self.assertIsNone(evaluation["first_error_line"])

    def test_code_line_is_not_reported_as_a_reasoning_step(self):
        review = {**REVIEW, "code_correct": False, "findings": [{"scope": "code", "line": 2, "error_type": "implementation_error", "reason": "无条件返回 True 忽略输入"}]}
        evaluation = self.run_case(passed=False, review=review)["evaluation"]
        self.assertFalse(evaluation["final_correct"])
        self.assertFalse(evaluation["code_correct"])
        self.assertEqual(evaluation["first_error_line"], 2)
        self.assertIsNone(evaluation["first_error_step"])
        self.assertEqual(evaluation["execution"]["tests"][0]["error"], "<hidden>")

    def test_deterministic_failure_cannot_be_overruled_by_model(self):
        evaluation = self.run_case(passed=False)["evaluation"]
        self.assertFalse(evaluation["final_correct"])
        self.assertFalse(evaluation["code_correct"])

    def test_reviewer_failure_keeps_tests_but_marks_logic_unknown(self):
        evaluation = self.run_case(steps=["说明"], review=Hy3APIError("unavailable"))["evaluation"]
        self.assertTrue(evaluation["final_correct"])
        self.assertIsNone(evaluation["code_correct"])
        self.assertIsNone(evaluation["process_correct"])
        self.assertFalse(evaluation["review_available"])

    def test_low_confidence_or_bad_locations_are_not_asserted_as_errors(self):
        for review in (
            {**REVIEW, "code_correct": False, "confidence": 0.2},
            {**REVIEW, "code_correct": False, "findings": [{"scope": "code", "line": 99, "error_type": "implementation_error", "reason": "错误行号"}]},
            {**REVIEW, "process_correct": False, "findings": [{"scope": "reasoning", "step": 99, "error_type": "algorithm_error", "reason": "不存在的步骤"}]},
        ):
            with self.subTest(review=review):
                evaluation = self.run_case(steps=["说明"], review=review)["evaluation"]
                self.assertIsNone(evaluation["code_correct"])
                self.assertIsNone(evaluation["first_error_line"])
                self.assertIsNone(evaluation["first_error_step"])

    def test_unavailable_sandbox_or_oracle_is_not_code_failure(self):
        evaluation = self.run_case(passed=False, harness="SandboxExit125:private backend message")["evaluation"]
        self.assertIsNone(evaluation["final_correct"])
        self.assertIsNone(evaluation["code_correct"])
        self.assertNotIn("private backend message", str(evaluation))
        evaluation = self.run_case(property_result={"enabled": True, "found": True, "counterexample": {"error": "ReferenceOracleError:failed"}})["evaluation"]
        self.assertIsNone(evaluation["final_correct"])
        self.assertIsNone(evaluation["hypothesis"]["counterexample"])

    @patch.dict("os.environ", {"SANDBOX_BACKEND": "local", "ALLOW_UNSAFE_LOCAL_EXECUTION": "true"})
    @patch("hy3_tracejudge.submissions.run_candidate")
    def test_worker_rechecks_docker_before_executing(self, candidate):
        with self.assertRaises(SubmissionSandboxRequired):
            evaluate_submission(get_problem("two_sum_exists"), {"code": CODE}, hypothesis_examples=1, update_phase=lambda _: None)
        candidate.assert_not_called()

    @patch.dict("os.environ", {"APP_ENV": "development", "SANDBOX_BACKEND": "local", "ALLOW_UNSAFE_LOCAL_EXECUTION": "true"})
    def test_worker_accepts_explicit_local_development_marker(self):
        require_submission_sandbox(allow_local=True)

    @patch.dict("os.environ", {"APP_ENV": "production", "SANDBOX_BACKEND": "local", "ALLOW_UNSAFE_LOCAL_EXECUTION": "true"})
    def test_worker_rejects_local_marker_in_production(self):
        with self.assertRaises(SubmissionSandboxRequired):
            require_submission_sandbox(allow_local=True)

    @patch("hy3_tracejudge.submissions.evaluate_submission", return_value={"source": "user_submission"})
    @patch("hy3_tracejudge.api.jobs.Hy3Client")
    def test_submission_runner_never_invokes_solver(self, client, evaluate):
        payload = {"code": CODE, "reasoning_steps": []}
        result = run_evaluation_job("two_sum_exists", 1, "submission", lambda _: None, submission=payload)
        self.assertEqual(result["source"], "user_submission")
        client.assert_not_called()
        self.assertEqual(evaluate.call_args.args[1], payload)

    def test_review_schema_rejects_coerced_boolean_verdicts(self):
        with self.assertRaises(Hy3APIError):
            normalize_review({**REVIEW, "code_correct": "true"}, CODE, [])

    def test_memory_queue_keeps_an_immutable_submission(self):
        ready = threading.Event()
        received = []
        def runner(problem_id, examples, mode, update_phase, *, submission):
            ready.wait(2)
            received.append(submission)
            return {"source": "user_submission"}
        manager = EvaluationJobManager(workers=1, queue_size=1, ttl_seconds=60, max_stored_jobs=10, runner=runner)
        payload = {"code": CODE, "reasoning_steps": []}
        try:
            job = manager.submit("two_sum_exists", 2, "submission", submission=payload)
            self.assertEqual(job["kind"], "code_submission")
            self.assertNotIn("submission", job)
            payload["code"] = "mutated"
            ready.set()
            deadline = time.monotonic() + 3
            while manager.snapshot(job["id"])["status"] not in {"succeeded", "failed"} and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(received[0]["code"], CODE)
        finally:
            ready.set()
            manager.shutdown()

    def test_memory_queue_does_not_expose_provider_credentials(self):
        received = []
        release = threading.Event()

        def runner(problem_id, examples, mode, update_phase, *, runtime):
            received.append(runtime)
            release.wait(timeout=1)
            return {"model": runtime["model"]}

        manager = EvaluationJobManager(
            workers=1, queue_size=1, ttl_seconds=60, max_stored_jobs=10, runner=runner
        )
        secret = "queue-provider-secret"
        try:
            job = manager.submit(
                "two_sum_exists", 1, "single",
                runtime={"model": "hy4-preview", "api_key": secret},
            )
            self.assertEqual(job["requested_model"], "hy4-preview")
            self.assertNotIn(secret, repr(job))
            release.set()
            deadline = time.monotonic() + 2
            snapshot = manager.snapshot(job["id"])
            while snapshot["status"] not in {"succeeded", "failed"} and time.monotonic() < deadline:
                time.sleep(0.01)
                snapshot = manager.snapshot(job["id"])
            self.assertEqual(received, [{"model": "hy4-preview", "api_key": secret}])
            self.assertNotIn(secret, repr(snapshot))
        finally:
            release.set()
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
