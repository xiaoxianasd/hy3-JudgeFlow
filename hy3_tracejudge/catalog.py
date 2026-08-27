from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "problems.json"


@lru_cache(maxsize=1)
def load_problems() -> list[dict[str, Any]]:
    with DATA_PATH.open("r", encoding="utf-8") as stream:
        problems = json.load(stream)
    validate_catalog(problems)
    return problems


def get_problem(problem_id: str) -> dict[str, Any]:
    for problem in load_problems():
        if problem["id"] == problem_id:
            return problem
    raise KeyError(f"Unknown problem id: {problem_id}")


def validate_catalog(problems: list[dict[str, Any]]) -> None:
    required = {
        "id",
        "title",
        "difficulty",
        "statement",
        "function_name",
        "public_tests",
        "hidden_tests",
        "reference_solution",
        "fault",
        "gold_steps",
        "rubric",
    }
    seen: set[str] = set()
    for problem in problems:
        missing = required - set(problem)
        if missing:
            raise ValueError(f"{problem.get('id', '<unknown>')} missing fields: {sorted(missing)}")
        if problem["id"] in seen:
            raise ValueError(f"Duplicate problem id: {problem['id']}")
        seen.add(problem["id"])
        if problem["difficulty"] not in {"easy", "medium", "hard"}:
            raise ValueError(f"Invalid difficulty for {problem['id']}")
        step_ids = [step["id"] for step in problem["gold_steps"]]
        if step_ids != list(range(1, len(step_ids) + 1)):
            raise ValueError(f"Non-contiguous gold steps for {problem['id']}")

