from __future__ import annotations

import copy
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
    """Build a deterministic, manually specified evaluator-validation suite.

    The mix intentionally becomes harder: easy has 3 sound / 1 wrong,
    medium has 2 sound / 1 unsupported-correct / 1 wrong, and hard has
    1 sound / 1 unsupported-correct / 2 wrong samples per problem.
    Auto-imported external problems (fault=None) are skipped: the fixture
    suite stays on hand-curated seed problems with reviewed single defects.
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
    return samples

