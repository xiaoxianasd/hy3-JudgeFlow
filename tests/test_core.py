from __future__ import annotations

import unittest

from hy3_tracejudge.catalog import get_problem, load_problems
from hy3_tracejudge.evaluator import evaluate_answer
from hy3_tracejudge.executor import reference_check
from hy3_tracejudge.fixtures import make_answer
from hy3_tracejudge.hy3_client import Hy3Client, Hy3Config
from hy3_tracejudge.property_testing import find_counterexample


class CatalogTests(unittest.TestCase):
    def test_all_reference_solutions_pass_fixed_tests(self) -> None:
        for problem in load_problems():
            with self.subTest(problem=problem["id"]):
                result = reference_check(problem)
                self.assertIsNone(result.harness_error)
                self.assertTrue(result.all_passed)


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
        evaluation = evaluate_answer(problem, answer, hypothesis_examples=8)
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
        self.assertEqual(fake.last_payload["chat_template_kwargs"]["reasoning_effort"], "high")
        self.assertEqual(metadata["request_id"], "test-id")

    def test_tokenhub_uses_top_level_reasoning_effort(self) -> None:
        problem = get_problem("two_sum_exists")
        answer = make_answer(problem, "gold")
        fake = FakeHy3(
            {"choices": [{"message": {"content": __import__("json").dumps(answer, ensure_ascii=False)}}]},
            base_url="https://tokenhub.tencentmaas.com/v1",
        )
        fake.solve(problem)
        self.assertEqual(fake.last_payload["reasoning_effort"], "high")
        self.assertNotIn("chat_template_kwargs", fake.last_payload)


if __name__ == "__main__":
    unittest.main()
