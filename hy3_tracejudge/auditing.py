"""File-based human review, bound to the exact evaluated record."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .evaluator import validate_evaluator


def record_hash(record: dict[str, Any]) -> str:
    payload = {key: record.get(key) for key in ("problem_id", "problem", "answer", "evaluation")}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def indexed_records(benchmark: dict[str, Any]) -> dict[str, dict[str, Any]]:
    records = {}
    for record in benchmark["records"]:
        sample_id = record.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id.strip() or sample_id in records:
            raise ValueError("基准中的 sample_id 必须非空且唯一")
        records[sample_id] = record
    return records


def build_audit_queue(benchmark: dict[str, Any]) -> list[dict[str, Any]]:
    """Export all completed samples, without predictions or fixture gold labels."""
    return [{
        "sample_id": sample_id,
        "record_sha256": record_hash(record),
        "problem_id": record["problem_id"],
        "problem": record.get("problem"),
        "answer": record["answer"],
        "annotator": "",
        "independent_human_audit": False,
        "final_answer_correct": None,
        "human_process_valid": None,
        "first_error_step": None,
        "evidence": "",
        "status": "pending",
    } for sample_id, record in indexed_records(benchmark).items()
        if "evaluation" in record and "answer" in record]


def summarize_audits(benchmark: dict[str, Any], annotations: list[dict[str, Any]]) -> dict[str, Any]:
    records = indexed_records(benchmark)
    reviewed = []
    seen = set()
    for annotation in annotations:
        sample_id = annotation.get("sample_id")
        if not isinstance(sample_id, str) or sample_id in seen or sample_id not in records:
            raise ValueError("人工记录包含重复或未知 sample_id")
        seen.add(sample_id)
        record = records[sample_id]
        if annotation.get("record_sha256") != record_hash(record):
            raise ValueError(f"{sample_id}: 记录指纹不匹配，请勿混用其他运行的标注")
        status = annotation.get("status")
        if status == "pending":
            continue
        if status != "completed":
            raise ValueError(f"{sample_id}: status 必须为 pending 或 completed")
        if annotation.get("independent_human_audit") is not True:
            raise ValueError(f"{sample_id}: 仅接受明确声明的独立人工复核")
        for field in ("annotator", "evidence"):
            if not isinstance(annotation.get(field), str) or not annotation[field].strip():
                raise ValueError(f"{sample_id}: 缺少 {field}")
        for field in ("final_answer_correct", "human_process_valid"):
            if type(annotation.get(field)) is not bool:
                raise ValueError(f"{sample_id}: {field} 必须是布尔值")
        step = annotation.get("first_error_step")
        valid = annotation["human_process_valid"]
        max_step = len(record["answer"].get("reasoning_steps", [])) + 1
        if valid and step is not None:
            raise ValueError(f"{sample_id}: 过程成立时不得标记首错")
        if not valid and (type(step) is not int or not 1 <= step <= max_step):
            raise ValueError(f"{sample_id}: 过程有错时需提供实际首错；未定位请保留 pending")
        reviewed.append({
            "evaluation": record["evaluation"],
            "ground_truth": {"final_correct": annotation["final_answer_correct"],
                             "process_valid": valid, "first_error_step": step},
        })
    completed_records = [r for r in records.values() if "evaluation" in r]
    flagged = {sid for sid, r in records.items() if r.get("evaluation", {}).get("final_correct") is True
               and r["evaluation"].get("process_correct") is False}
    finished = {a["sample_id"] for a in annotations if a.get("status") == "completed"}
    metrics = validate_evaluator(reviewed)
    # Empty cohorts provide no estimate, not a measured zero error rate.
    if not metrics["wrong_answer_samples"]:
        metrics["process_problem_detection_rate"] = None
        metrics["exact_step_localization_accuracy"] = None
    if not metrics["flagged_answer_correct_samples"]:
        metrics["flagged_real_issue_ratio"] = None
    n = metrics["flagged_answer_correct_samples"]
    metrics["flagged_false_positive_ratio"] = metrics["flagged_false_positive_count"] / n if n else None
    return {
        "run_type": "independent_human_audit_summary",
        "completed_evaluations": len(completed_records),
        "reviewed_samples": len(reviewed),
        "pending_or_unreviewed_samples": len(completed_records) - len(reviewed),
        "audit_coverage": len(reviewed) / len(completed_records) if completed_records else 0.0,
        "predicted_flagged_correct_samples": len(flagged),
        "reviewed_predicted_flagged_samples": len(flagged & finished),
        "flagged_audit_coverage": len(flagged & finished) / len(flagged) if flagged else None,
        "metrics": metrics,
        "note": "指标仅适用于已完成人工复核的样本；未复核与缺失样本不算正确或误报。",
    }
