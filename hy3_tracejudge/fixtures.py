from __future__ import annotations

import copy
import json
from typing import Any


def make_answer(problem: dict[str, Any], profile: str) -> dict[str, Any]:
    steps = copy.deepcopy(problem["gold_steps"])
    code = problem["reference_solution"]
    if profile in {"wrong", "unsupported_correct"}:
        fault = problem["fault"]
        step = next(item for item in steps if item["id"] == fault["step"])
        step["content"] = fault["content"]
        if profile == "wrong":
            code = fault["solution"]
    return {
        "reasoning_steps": steps,
        "complexity": copy.deepcopy(problem["expected_complexity"]),
        "edge_cases": copy.deepcopy(problem["boundary_cases"]),
        "code": code,
        "final_answer": "已给出可执行 solve_case 实现。",
    }


def build_labeled_samples(problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build deduplicated controlled and adversarial evaluator samples.

    Keep the original IDs of retained samples for traceability; the historical
    repeated difficulty mix is removed before adding adversarial variants.
    External problems without controlled labels are not fabricated into gold.
    """
    problems = [problem for problem in problems if problem.get("fault")]
    profiles = {
        "easy": ["gold", "gold", "gold", "wrong"],
        "medium": ["gold", "gold", "unsupported_correct", "wrong"],
        "hard": ["gold", "unsupported_correct", "wrong", "wrong"],
    }
    samples: list[dict[str, Any]] = []
    for problem in problems:
        for index, profile in enumerate(profiles[problem["difficulty"]], start=1):
            process_valid = profile == "gold"
            final_correct = profile != "wrong"
            samples.append(
                {
                    "sample_id": f"{problem['id']}-{index:02d}-{profile}",
                    "problem_id": problem["id"],
                    "difficulty": problem["difficulty"],
                    "profile": profile,
                    "answer": make_answer(problem, profile),
                    "ground_truth": {
                        "final_correct": final_correct,
                        "process_valid": process_valid,
                        "first_error_step": None if process_valid else problem["fault"]["step"],
                        "error_type": None if process_valid else problem["fault"]["error_type"],
                        "annotation_basis": (
                            "reference trace and reference code"
                            if process_valid
                            else "single injected, manually reviewed defect"
                        ),
                    },
                }
            )
    # Repeated identical gold/wrong trajectories must not inflate the denominator.
    unique = {}
    for sample in samples:
        key = (sample["problem_id"], json.dumps(sample["answer"], sort_keys=True, ensure_ascii=False))
        unique.setdefault(key, sample)
    samples = list(unique.values())
    for problem in problems:
        gold = next(s for s in samples if s["problem_id"] == problem["id"] and s["profile"] == "gold")
        keyword = copy.deepcopy(gold)
        keyword.update(sample_id=f"{problem['id']}-keyword-only", profile="keyword_only")
        for step in keyword["answer"]["reasoning_steps"]:
            criterion = next(c for c in problem["rubric"] if c["stage"] == step["stage"])
            step["title"] = "术语堆砌"
            step["content"] = criterion["evidence_any"][0] + "。因为结论成立，所以结论成立，无需推导。"
        keyword["ground_truth"].update(process_valid=False, first_error_step=1,
                                       error_type="circular_reasoning",
                                       annotation_basis="受控变换：保留正确代码，将各步替换为术语及循环论证；非独立人工抽检")
        samples.append(keyword)
        forbidden = next((c for c in problem["rubric"] if c.get("forbidden")), None)
        if forbidden:
            negated = copy.deepcopy(gold)
            negated.update(sample_id=f"{problem['id']}-negated-fault", profile="negated_fault")
            step = next(s for s in negated["answer"]["reasoning_steps"] if s["stage"] == forbidden["stage"])
            step["content"] += f" 错误示例：‘{forbidden['forbidden'][0]}’。该说法不成立，应采用前述正确算法。"
            negated["ground_truth"]["annotation_basis"] = "受控变换：正确过程末尾明确否定错误示例；非独立人工抽检"
            samples.append(negated)
    return samples
