from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for value in values:
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def write_results_csv(path: Path, values: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "sample_id",
        "problem_id",
        "difficulty",
        "final_correct",
        "process_correct",
        "unsupported_correct",
        "first_error_step",
        "error_types",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for item in values:
            evaluation = item.get("evaluation", item)
            writer.writerow(
                {
                    "sample_id": item.get("sample_id", ""),
                    "problem_id": item.get("problem_id", evaluation.get("problem_id", "")),
                    "difficulty": item.get("difficulty", evaluation.get("difficulty", "")),
                    "final_correct": evaluation.get("final_correct"),
                    "process_correct": evaluation.get("process_correct"),
                    "unsupported_correct": evaluation.get("unsupported_correct"),
                    "first_error_step": evaluation.get("first_error_step"),
                    "error_types": "|".join(evaluation.get("error_types", [])),
                }
            )
