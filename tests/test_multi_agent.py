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


if __name__ == "__main__":
    unittest.main()
