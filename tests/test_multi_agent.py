from __future__ import annotations

import threading
import unittest

from hy3_tracejudge.catalog import get_problem
from hy3_tracejudge.evaluator import evaluate_answer
from hy3_tracejudge.fixtures import make_answer
from hy3_tracejudge.multi_agent import SPECIALISTS, run_multi_agent_review


class FakeMultiAgentClient:
    """Deterministic double: tests orchestration without spending Hy3 tokens."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.arbitration_calls = 0
        self._lock = threading.Lock()

    def review_stage(self, problem, answer, evidence, *, agent_name, stage, responsibility):
        with self._lock:
            self.calls.append(agent_name)
        invalid = stage == "algorithm"
        return (
            {
                "valid": not invalid,
                "reviewed_steps": [2],
                "first_error_step": 2 if invalid else None,
                "error_type": "theorem_misuse" if invalid else None,
                "reason": "贪心选择缺少交换论证" if invalid else "本阶段证据成立",
                "evidence": ["fixture specialist evidence"],
                "inherited_from_step": None,
                "confidence": 0.96,
            },
            {"usage": {"prompt_tokens": 10, "completion_tokens": 2}},
        )

    def arbitrate_reviews(self, problem, answer, evidence, specialist_reviews, conflicts):
        self.arbitration_calls += 1
        return (
            {
                "process_correct": False,
                "first_error_step": 2,
                "error_types": ["theorem_misuse"],
                "supporting_agents": ["algorithm_agent"],
                "rationale": "算法专家给出了最早的语义错误证据。",
                "confidence": 0.98,
            },
            {"usage": {"prompt_tokens": 8, "completion_tokens": 2}},
        )


class FakeAdapterMismatchClient:
    """Reproduces the former MBPP source-signature false positive."""

    def review_stage(self, problem, answer, evidence, *, agent_name, stage, responsibility):
        invalid = stage == "understanding"
        return (
            {
                "valid": not invalid,
                "reviewed_steps": [1] if invalid else [],
                "first_error_step": 1 if invalid else None,
                "error_type": "problem_misread" if invalid else None,
                "reason": (
                    "原题断言 is_Diff(12345) 直接接收整数，而步骤1使用 case 字典。"
                    if invalid
                    else "本阶段未发现问题"
                ),
                "evidence": ["原函数直接参数与 case 形式不同"] if invalid else [],
                "inherited_from_step": None,
                "confidence": 0.97,
            },
            {"usage": {"prompt_tokens": 5, "completion_tokens": 2}},
        )

    def review(self, problem, answer, evidence):
        return (
            {
                "step_reviews": [
                    {
                        "step": 1,
                        "valid": False,
                        "error_type": "problem_misread",
                        "reason": "原函数直接接收整数，但答案使用 solve_case 的 case 字典。",
                    }
                ],
                "process_correct": False,
                "first_error_step": 1,
                "error_types": ["problem_misread"],
                "confidence": 0.97,
                "rationale": "原题直接参数调用与 case 字典接口不同。",
            },
            {"usage": {"prompt_tokens": 5, "completion_tokens": 2}},
        )


def external_adapter_answer(problem):
    return {
        "reasoning_steps": [
            {
                "id": 1,
                "stage": "understanding",
                "title": "题意与建模",
                "content": "输入是 case 字典，其中 n 为待判断的整数。",
            },
            {"id": 2, "stage": "algorithm", "title": "算法", "content": "检查 n 是否能被 11 整除。"},
            {"id": 3, "stage": "proof", "title": "正确性", "content": "余数为零当且仅当可以整除。"},
            {"id": 4, "stage": "complexity", "title": "复杂度", "content": "时间和空间均为 O(1)。"},
            {"id": 5, "stage": "boundary", "title": "边界", "content": "覆盖零、负数和正数。"},
        ],
        "complexity": {"time": "O(1)", "space": "O(1)"},
        "edge_cases": ["0", "负数"],
        "code": problem["reference_solution"],
        "final_answer": "按统一接口返回能否被 11 整除。",
    }


class MultiAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.problem = get_problem("two_sum_exists")
        self.answer = make_answer(self.problem, "gold")
        self.evidence = evaluate_answer(
            self.problem,
            self.answer,
            hypothesis_examples=5,
        )
        self.provisional = {
            "criteria": self.evidence["criteria"],
            "execution": self.evidence["execution"],
            "hypothesis": self.evidence["hypothesis"],
        }

    def test_supervisor_dispatches_five_specialists_and_localizes_error(self) -> None:
        client = FakeMultiAgentClient()
        result = run_multi_agent_review(
            client,
            self.problem,
            self.answer,
            self.provisional,
            mode="supervisor",
        )
        self.assertCountEqual(client.calls, [item.name for item in SPECIALISTS])
        self.assertEqual(result["model_calls"], 5)
        self.assertFalse(result["decision"]["process_correct"])
        self.assertEqual(result["decision"]["first_error_step"], 2)
        self.assertIn("theorem_misuse", result["decision"]["error_types"])
        self.assertEqual(client.arbitration_calls, 0)

    def test_swarm_adds_one_bounded_arbitration_round(self) -> None:
        client = FakeMultiAgentClient()
        result = run_multi_agent_review(
            client,
            self.problem,
            self.answer,
            self.provisional,
            mode="swarm",
        )
        self.assertEqual(client.arbitration_calls, 1)
        self.assertEqual(result["model_calls"], 6)
        self.assertEqual(result["decision"]["supporting_sources"], ["algorithm_agent"])

    def test_evaluator_exposes_supervisor_trace(self) -> None:
        client = FakeMultiAgentClient()
        evaluation = evaluate_answer(
            self.problem,
            self.answer,
            hypothesis_examples=5,
            hy3_client=client,  # type: ignore[arg-type]
            review_mode="supervisor",
        )
        self.assertTrue(evaluation["final_correct"])
        self.assertFalse(evaluation["process_correct"])
        self.assertTrue(evaluation["unsupported_correct"])
        self.assertEqual(evaluation["first_error_step"], 2)
        self.assertEqual(len(evaluation["orchestration"]["specialist_reviews"]), 5)

    def test_supervisor_ignores_declared_adapter_contract_false_positive(self) -> None:
        problem = get_problem("mbpp_Mbpp/77")
        evaluation = evaluate_answer(
            problem,
            external_adapter_answer(problem),
            use_hypothesis=False,
            hy3_client=FakeAdapterMismatchClient(),  # type: ignore[arg-type]
            review_mode="supervisor",
        )
        self.assertTrue(evaluation["final_correct"])
        self.assertIsNone(evaluation["process_correct"])
        self.assertIsNone(evaluation["unsupported_correct"])
        self.assertIsNone(evaluation["first_error_step"])
        understanding = evaluation["orchestration"]["specialist_reviews"][0]
        self.assertEqual(
            understanding["ignored_by_supervisor"],
            "adapter_contract_false_positive",
        )
        self.assertIn(
            "adapter_contract_false_positive",
            {item["kind"] for item in evaluation["orchestration"]["conflicts"]},
        )

    def test_single_review_ignores_declared_adapter_contract_false_positive(self) -> None:
        problem = get_problem("mbpp_Mbpp/77")
        evaluation = evaluate_answer(
            problem,
            external_adapter_answer(problem),
            use_hypothesis=False,
            hy3_client=FakeAdapterMismatchClient(),  # type: ignore[arg-type]
            review_mode="single",
        )
        self.assertTrue(evaluation["final_correct"])
        self.assertIsNone(evaluation["process_correct"])
        self.assertIsNone(evaluation["unsupported_correct"])
        self.assertIsNone(evaluation["first_error_step"])
        self.assertEqual(
            evaluation["hy3_review"]["ignored_by_supervisor"],
            "adapter_contract_false_positive",
        )


if __name__ == "__main__":
    unittest.main()
