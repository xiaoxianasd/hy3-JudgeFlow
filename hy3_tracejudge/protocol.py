from __future__ import annotations

import json
import re
from typing import Any


REQUIRED_ANSWER_FIELDS = {
    "reasoning_steps",
    "complexity",
    "edge_cases",
    "code",
    "final_answer",
}


def validate_answer_shape(answer: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(answer, dict):
        return ["答案不是 JSON 对象"]
    missing = REQUIRED_ANSWER_FIELDS - set(answer)
    if missing:
        errors.append(f"缺少字段: {', '.join(sorted(missing))}")
    steps = answer.get("reasoning_steps")
    if not isinstance(steps, list) or not steps:
        errors.append("reasoning_steps 必须是非空数组")
    else:
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                errors.append(f"第 {index} 步不是对象")
                continue
            for field in ("id", "stage", "title", "content"):
                if field not in step:
                    errors.append(f"第 {index} 步缺少 {field}")
            if type(step.get("id")) is not int or step["id"] != index:
                errors.append(f"第 {index} 步 id 必须为从 1 开始连续编号的整数")
            for field in ("stage", "title", "content"):
                if field in step and not isinstance(step[field], str):
                    errors.append(f"第 {index} 步 {field} 必须是字符串")
    if "code" in answer and not isinstance(answer["code"], str):
        errors.append("code 必须是字符串")
    return errors


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract the first balanced JSON object from a model response."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    candidates = [fenced.group(1)] if fenced else []
    start = text.find("{")
    if start >= 0:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : index + 1])
                    break
    failures: list[str] = []
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError as exc:
            failures.append(str(exc))
    raise ValueError("无法从模型输出解析 JSON" + (f": {failures[-1]}" if failures else ""))


def answer_schema_example(function_name: str) -> str:
    schema = {
        "reasoning_steps": [
            {"id": 1, "stage": "understanding", "title": "题意与建模", "content": "..."},
            {"id": 2, "stage": "algorithm", "title": "算法", "content": "..."},
            {"id": 3, "stage": "proof", "title": "正确性", "content": "..."},
            {"id": 4, "stage": "complexity", "title": "复杂度", "content": "..."},
            {"id": 5, "stage": "boundary", "title": "边界", "content": "..."},
        ],
        "complexity": {"time": "O(...) ", "space": "O(...)"},
        "edge_cases": ["..."],
        "code": f"def {function_name}(case):\\n    ...",
        "final_answer": "实现与结论摘要",
    }
    return json.dumps(schema, ensure_ascii=False, indent=2)
