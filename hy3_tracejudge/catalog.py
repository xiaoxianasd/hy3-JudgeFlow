from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "problems.json"
EXTERNAL_PATH = ROOT / "data" / "problems_external.json"
ADAPTER_HEADING = "【TraceJudge 统一执行接口】"


def normalize_external_problem(problem: dict[str, Any]) -> dict[str, Any]:
    """Add an explicit, shared execution contract to imported function tasks."""
    normalized = dict(problem)
    source_statement = str(
        normalized.get("source_statement") or normalized.get("statement", "")
    ).strip()
    input_schema = normalized.get("input_schema", {})
    case_fields = list(input_schema) if isinstance(input_schema, dict) else []
    source_function = normalized.get("source_function_name")
    contract = normalized.get("adapter_contract")
    if not isinstance(contract, dict):
        contract = {
            "kind": "keyword_case_adapter",
            "entrypoint": "solve_case(case)",
            "source_entrypoint": source_function,
            "case_fields": case_fields,
            "equivalence": (
                "原题中的直接参数调用描述任务语义；TraceJudge 将这些参数按名称装入 "
                "case 字典，两种接口语义等价。"
            ),
        }
    fields = "、".join(str(item) for item in contract.get("case_fields", case_fields)) or "无"
    interface_note = (
        f"{ADAPTER_HEADING}\n"
        "原题中的函数名、直接位置参数和 assert 仅用于说明任务语义。"
        "在本系统中必须实现 solve_case(case)，case 是 JSON 字典，"
        f"字段为：{fields}。solve_case 返回原题函数应返回的结果。"
        "这是执行适配而非题意变更，不得将合法的 case 字典描述判为题意误读。"
    )
    normalized["source_statement"] = source_statement
    normalized["adapter_contract"] = contract
    normalized["statement"] = f"{source_statement}\n\n{interface_note}"
    title = str(normalized.get("title", "")).strip().strip('"').strip()
    if not title:
        title = next(
            (
                line.strip().strip('"').strip()
                for line in source_statement.splitlines()
                if line.strip().strip('"').strip()
                and not line.lstrip().startswith("assert ")
            ),
            normalized.get("id", "未命名题目"),
        )
    normalized["title"] = title[:100]
    return normalized


@lru_cache(maxsize=1)
def load_problems() -> list[dict[str, Any]]:
    with DATA_PATH.open("r", encoding="utf-8") as stream:
        problems = json.load(stream)
    validate_catalog(problems)
    if EXTERNAL_PATH.exists():
        external = [
            normalize_external_problem(item)
            for item in json.loads(EXTERNAL_PATH.read_text(encoding="utf-8"))
        ]
        validate_catalog(external)
        seed_ids = {problem["id"] for problem in problems}
        for problem in external:
            if problem["id"] in seed_ids:
                raise ValueError(f"External problem id collides with seed: {problem['id']}")
        problems.extend(external)
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
