"""Merge targeted fixture retries without double-counting sample IDs."""
from __future__ import annotations

import copy
import json
from typing import Any

from .detection import summarize_detection
from .evaluator import summarize_results, validate_evaluator


def merge_fixture_reports(named_reports: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    attempts: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for source, report in named_reports:
        if report.get("run_type") != "evaluator_validation_fixtures":
            raise ValueError(f"{source}: 不是构造集评测报告")
        for record in report.get("records", []):
            attempts.setdefault(record["sample_id"], []).append((source, record))

    merged = []
    for sample_id, sample_attempts in attempts.items():
        signatures = {
            json.dumps({"answer": record.get("answer"), "ground_truth": record.get("ground_truth")},
                       ensure_ascii=False, sort_keys=True)
            for _, record in sample_attempts
        }
        if len(signatures) != 1:
            raise ValueError(f"{sample_id}: 重试之间的答案或金标不一致")
        conclusive = [(source, record) for source, record in sample_attempts
                      if type(record.get("evaluation", {}).get("process_correct")) is bool]
        conclusions = {(record["evaluation"]["process_correct"],
                        record["evaluation"].get("first_error_step")) for _, record in conclusive}
        if len(conclusions) > 1:
            raise ValueError(f"{sample_id}: 多次明确评审结论冲突，必须人工裁决")
        selected_source, selected = (conclusive or sample_attempts)[-1]
        selected = copy.deepcopy(selected)
        selected["selected_attempt_source"] = selected_source
        selected["evaluation_attempts"] = [{
            "source": source,
            "process_correct": record.get("evaluation", {}).get("process_correct"),
            "first_error_step": record.get("evaluation", {}).get("first_error_step"),
            "error_types": record.get("evaluation", {}).get("error_types", []),
            "error": record.get("evaluation", {}).get("hy3_review", {}).get("error"),
        } for source, record in sample_attempts]
        merged.append(selected)

    evaluations = [{**record["evaluation"], "difficulty": record["difficulty"]} for record in merged]
    models = sorted({
        metadata["model"]
        for record in merged
        for metadata in [record.get("evaluation", {}).get("hy3_review_metadata")]
        if isinstance(metadata, dict) and isinstance(metadata.get("model"), str)
    })
    return {
        "run_type": "merged_evaluator_validation_fixtures",
        "models": models,
        "merge_policy": "sample_id去重；优先选择明确结论；多次明确结论冲突时拒绝自动合并",
        "source_reports": [source for source, _ in named_reports],
        "summary": summarize_results(evaluations),
        "validity": validate_evaluator(merged),
        "process_detection": summarize_detection(merged),
        "records": merged,
    }
