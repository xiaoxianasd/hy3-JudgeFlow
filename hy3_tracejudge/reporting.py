from __future__ import annotations

import csv
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .verdicts import localization_status, process_status


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
        "process_status",
        "failure_owner",
        "failure_owner_status",
        "localization_status",
        "assessment_note",
        "unsupported_correct",
        "first_error_step",
        "error_types",
        "run_error",
        "status",
        "attempts",
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
                    "process_status": evaluation.get("process_status", process_status(evaluation.get("process_correct"))),
                    "failure_owner": evaluation.get("failure_owner", item.get("failure_owner", "")),
                    "failure_owner_status": evaluation.get(
                        "failure_owner_status", item.get("failure_owner_status", "")
                    ),
                    "localization_status": evaluation.get("localization_status", localization_status(evaluation.get("process_correct"), evaluation.get("first_error_step"))),
                    "assessment_note": evaluation.get("assessment_note", ""),
                    "unsupported_correct": evaluation.get("unsupported_correct"),
                    "first_error_step": evaluation.get("first_error_step"),
                    "error_types": "|".join(evaluation.get("error_types", [])),
                    "run_error": item.get("run_error", ""),
                    "status": item.get("status", ""),
                    "attempts": item.get("attempts", ""),
                }
            )
