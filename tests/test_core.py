from __future__ import annotations

import unittest
from unittest.mock import patch

from hy3_tracejudge.catalog import get_problem, load_problems
from hy3_tracejudge.evaluator import evaluate_answer
from hy3_tracejudge.executor import reference_check
from hy3_tracejudge.fixtures import make_answer
from hy3_tracejudge.hy3_client import Hy3APIError, Hy3Client, Hy3Config, public_model_error
from hy3_tracejudge.property_testing import find_counterexample


class CatalogTests(unittest.TestCase):
    def test_all_reference_solutions_pass_fixed_tests(self) -> None:
        for problem in load_problems():
            if problem.get("tier") == "external":
                continue  # 导入时已逐题沙盒验证；这里仅全量覆盖种子题
            with self.subTest(problem=problem["id"]):
                result = reference_check(problem)
                self.assertIsNone(result.harness_error)
                self.assertTrue(result.all_passed)

    def test_imported_external_problems_spot_check(self) -> None:
        external = [p for p in load_problems() if p.get("tier") == "external"][:5]
        for problem in external:
            with self.subTest(problem=problem["id"]):
                result = reference_check(problem)
                self.assertIsNone(result.harness_error)
                self.assertTrue(result.all_passed)

    def test_external_problem_declares_normalized_adapter_contract(self) -> None:
        problem = get_problem("mbpp_Mbpp/77")
        self.assertEqual(problem["adapter_contract"]["entrypoint"], "solve_case(case)")
        self.assertEqual(problem["adapter_contract"]["case_fields"], ["n"])
        self.assertIn("TraceJudge 统一执行接口", problem["statement"])
        self.assertIn("不得将合法的 case 字典描述判为题意误读", problem["statement"])
        self.assertNotIn("TraceJudge 统一执行接口", problem["source_statement"])


class PropertyTestingTests(unittest.TestCase):
    def test_hypothesis_finds_and_shrinks_fault(self) -> None:
        problem = get_problem("two_sum_exists")
        result = find_counterexample(problem, problem["fault"]["solution"], max_examples=35)
        self.assertTrue(result["found"])
        self.assertIsInstance(result["counterexample"]["case"], dict)

    def test_reference_has_no_counterexample_in_budget(self) -> None:
        problem = get_problem("bracket_balance")
        result = find_counterexample(problem, problem["reference_solution"], max_examples=12)
        self.assertFalse(result["found"])


class EvaluatorTests(unittest.TestCase):
    def test_answer_correct_but_process_invalid_is_identified(self) -> None:
        problem = get_problem("coin_change")
        answer = make_answer(problem, "unsupported_correct")
        review = {"process_correct": False, "first_error_step": 2,
                  "error_types": ["theorem_misuse"], "confidence": 0.95,
                  "rationale": "一般币制下最大硬币贪心不成立；1、3、4 面额凑 6 的最优解是 3+3。"}
        with patch.object(Hy3Client, "review", return_value=(review, {})):
            evaluation = evaluate_answer(problem, answer, hypothesis_examples=8,
                                         hy3_client=Hy3Client(Hy3Config()))
        self.assertTrue(evaluation["final_correct"])
        self.assertFalse(evaluation["process_correct"])
        self.assertTrue(evaluation["unsupported_correct"])
        self.assertEqual(evaluation["first_error_step"], problem["fault"]["step"])

    def test_hypothesis_rejects_code_that_passes_all_fixed_tests(self) -> None:
        problem = get_problem("two_sum_exists")
        subtly_wrong = """def solve_case(case):
    seen = set()
    target = case['target']
    for value in case['nums']:
        if value == 0:
            continue
        if target - value in seen:
            return True
        seen.add(value)
    return False"""
        answer = make_answer(problem, "gold")
        answer["code"] = subtly_wrong
        evaluation = evaluate_answer(problem, answer, hypothesis_examples=20)
        self.assertTrue(evaluation["execution"]["all_passed"])
        self.assertTrue(evaluation["hypothesis"]["found"])
        self.assertFalse(evaluation["final_correct"])
        self.assertEqual(evaluation["error_types"], ["implementation_error"])


class FakeHy3(Hy3Client):
    def __init__(self, response: dict, base_url: str = "http://example.invalid/v1"):
        super().__init__(Hy3Config(base_url=base_url))
        self.response = response
        self.last_path = ""
        self.last_payload = None

    def _request(self, path, payload=None):
        self.last_path = path
        self.last_payload = payload
        return self.response


class Hy3ClientTests(unittest.TestCase):
    def test_public_model_error_exposes_status_without_provider_body(self) -> None:
        error = Hy3APIError(
            "Hy3 HTTP 429: secret provider response",
            retryable=True,
            status_code=429,
        )
        public = public_model_error(error, "hy4-preview", attempts=2, max_attempts=2)
        self.assertEqual(public["code"], "upstream_rate_limited")
        self.assertEqual(public["upstream_status"], 429)
        self.assertTrue(public["retryable"])
        self.assertIn("已自动尝试 2 次", public["message"])
        self.assertNotIn("secret provider response", str(public))

    def test_length_limited_generation_has_actionable_public_error(self) -> None:
        error = Hy3APIError(
            "Hy3 solver returned reasoning_content but no final content; finish_reason=length",
            failure_owner="model",
        )
        public = public_model_error(error, "hy3", attempts=3, max_attempts=3)
        self.assertEqual(public["code"], "model_output_truncated")
        self.assertIn("token 上限", public["message"])
        self.assertIn("已自动尝试 3 次", public["message"])

    def test_probe_performs_real_inference(self) -> None:
        fake = FakeHy3({"data": [{"id": "hy3"}]})
        responses = iter([
            {"data": [{"id": "hy3"}]},
            {"choices": [{"message": {"content": '{"ok":true}'}}]},
        ])
        fake._request = lambda path, payload=None: next(responses)  # type: ignore[method-assign]
        result = fake.probe()
        self.assertTrue(result["ok"])
        self.assertEqual(result["probe"], "inference")

    def test_all_review_protocols_accept_null_and_reject_string_booleans(self) -> None:
        import json
        problem = get_problem("two_sum_exists")
        answer = make_answer(problem, "gold")
        for field in ("process_correct", "valid"):
            for verdict in (None, "false"):
                fake = FakeHy3({"choices": [{"message": {"content": json.dumps({field: verdict})}}]})
                calls = [lambda: fake.review_stage(problem, answer, {}, agent_name="proof", stage="proof", responsibility="test")] if field == "valid" else [
                    lambda: fake.review(problem, answer, {}),
                    lambda: fake.arbitrate_reviews(problem, answer, {}, [], []),
                ]
                for call in calls:
                    if verdict is None:
                        self.assertIsNone(call()[0][field])
                    else:
                        with self.assertRaises(Hy3APIError):
                            call()

    def test_invalid_structured_json_is_classified_as_upstream_error(self) -> None:
        problem = get_problem("mbpp_Mbpp/123")
        fake = FakeHy3(
            {
                "choices": [{"message": {"content": "not a JSON object"}}],
                "usage": {"completion_tokens": 4},
            }
        )
        with self.assertRaisesRegex(Hy3APIError, "invalid structured JSON") as raised:
            fake.solve(problem)
        self.assertEqual(raised.exception.failure_owner, "model")

    def test_solve_calls_openai_compatible_hy3_payload(self) -> None:
        problem = get_problem("two_sum_exists")
        answer = make_answer(problem, "gold")
        fake = FakeHy3(
            {
                "id": "test-id",
                "model": "hy3",
                "choices": [{"message": {"content": __import__("json").dumps(answer, ensure_ascii=False)}}],
                "usage": {"completion_tokens": 10},
            }
        )
        parsed, metadata = fake.solve(problem)
        self.assertEqual(parsed["code"], answer["code"])
        self.assertEqual(fake.last_path, "/chat/completions")
        self.assertEqual(fake.last_payload["model"], "hy3")
        self.assertEqual(fake.last_payload["chat_template_kwargs"]["reasoning_effort"], "low")
        self.assertEqual(metadata["request_id"], "test-id")
        system_prompt = fake.last_payload["messages"][0]["content"]
        self.assertIn("允许 import", system_prompt)
        self.assertIn("collections", system_prompt)
        self.assertIn("禁止其他模块", system_prompt)

    def test_tokenhub_uses_top_level_reasoning_effort(self) -> None:
        problem = get_problem("two_sum_exists")
        answer = make_answer(problem, "gold")
        fake = FakeHy3(
            {"choices": [{"message": {"content": __import__("json").dumps(answer, ensure_ascii=False)}}]},
            base_url="https://tokenhub.tencentmaas.com/v1",
        )
        fake.solve(problem)
        self.assertEqual(fake.last_payload["reasoning_effort"], "low")
        self.assertEqual(fake.last_payload["response_format"], {"type": "json_object"})
        self.assertNotIn("chat_template_kwargs", fake.last_payload)


if __name__ == "__main__":
    unittest.main()
