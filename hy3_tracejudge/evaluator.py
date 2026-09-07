from __future__ import annotations

import json
import math
from collections import Counter
from typing import Any

from .executor import run_candidate
from .hy3_client import Hy3Client
from .multi_agent import _build_decision, run_multi_agent_review, run_single_agent_review
from .property_testing import find_counterexample
from .protocol import validate_answer_shape
from .verdicts import test_verdict, unsupported_verdict


ERROR_LABELS = {
    "format_error": "格式不符",
    "problem_misread": "题意误读",
    "concept_error": "概念理解错误",
    "algorithm_error": "算法逻辑错误",
    "theorem_misuse": "定理/贪心误用",
    "condition_omission": "条件或边界遗漏",
    "calculation_error": "计算错误",
    "unjustified_jump": "跳步推导",
    "circular_reasoning": "循环论证",
    "complexity_error": "复杂度分析错误",
    "hallucination": "幻觉/无依据声明",
    "implementation_error": "实现逻辑错误",
    "final_answer_error": "最终答案错误",
}


def _stage_text(answer: dict[str, Any], stage: str) -> tuple[str, int | None]:
    chunks: list[str] = []
    first_id: int | None = None
    for step in answer.get("reasoning_steps", []):
        if isinstance(step, dict) and step.get("stage") == stage:
            if first_id is None and isinstance(step.get("id"), int):
                first_id = step["id"]
            chunks.extend([str(step.get("title", "")), str(step.get("content", ""))])
    if stage == "complexity":
        chunks.append(json.dumps(answer.get("complexity", {}), ensure_ascii=False))
    if stage == "boundary":
        chunks.append(json.dumps(answer.get("edge_cases", []), ensure_ascii=False))
    return " ".join(chunks).lower().replace(" ", ""), first_id


def _criterion_result(answer: dict[str, Any], criterion: dict[str, Any]) -> dict[str, Any]:
    text, source_step = _stage_text(answer, criterion["stage"])
    forbidden_hit = next(
        (term for term in criterion.get("forbidden", []) if term.lower().replace(" ", "") in text),
        None,
    )
    evidence_hit = next(
        (term for term in criterion.get("evidence_any", []) if term.lower().replace(" ", "") in text),
        None,
    )
    passed = forbidden_hit is None and evidence_hit is not None
    reason = "evidence:" + evidence_hit if passed else (
        "forbidden:" + forbidden_hit if forbidden_hit else "missing_evidence"
    )
    return {
        "stage": criterion["stage"],
        "expected_step": criterion["step"],
        "source_step": source_step or criterion["step"],
        "description": criterion["description"],
        # Lexical matches are search hints, not a semantic proof or disproof.
        "passed": None,
        "signal": "matched" if passed else "review_needed",
        "evidence_kind": "lexical_hint",
        "reason": reason,
        "error_type": None,
        "suggested_error_type": None if passed else criterion["error_type"],
    }


def evaluate_answer(
    problem: dict[str, Any],
    answer: dict[str, Any],
    *,
    reveal_hidden: bool = True,
    use_hypothesis: bool = True,
    hypothesis_examples: int = 60,
    hy3_client: Hy3Client | None = None,
    review_mode: str = "single",
) -> dict[str, Any]:
    if review_mode not in {"single", "supervisor", "swarm"}:
        raise ValueError(f"Unsupported review mode: {review_mode}")
    shape_errors = validate_answer_shape(answer)
    if shape_errors:
        return {
            "problem_id": problem["id"],
            "difficulty": problem["difficulty"],
            "final_correct": None,
            "process_correct": False,
            "process_status": "invalid",
            "localization_status": "unlocalized",
            "assessment_note": "答案格式不符合协议，未执行测试；格式问题不等同于第一步题意错误。",
            "review_coverage": {"expected": 0, "completed": 0, "conclusive": 0},
            "unsupported_correct": None,
            "first_error_step": None,
            "error_types": ["format_error"],
            "error_labels": [ERROR_LABELS["format_error"]],
            "format_errors": shape_errors,
            "criteria": [],
            "execution": None,
            "hypothesis": None,
            "hy3_review": None,
            "hy3_review_metadata": None,
            "review_mode": review_mode,
            "orchestration": None,
        }

    execution = run_candidate(problem, answer["code"])
    hypothesis = None
    if use_hypothesis and not execution.harness_error:
        hypothesis = find_counterexample(
            problem,
            answer["code"],
            max_examples=hypothesis_examples,
        )
    criteria = [_criterion_result(answer, item) for item in problem["rubric"]]
    provisional = {
        "criteria": criteria,
        "execution": execution.to_dict(reveal_hidden=True),
        "hypothesis": hypothesis,
    }
    hy3_review = None
    hy3_metadata = None
    orchestration = None
    if hy3_client is None:
        decision, _ = _build_decision(problem, answer, provisional, [], specialists=())
    else:
        if review_mode == "single":
            hy3_review, hy3_metadata, decision = run_single_agent_review(
                hy3_client, problem, answer, provisional
            )
        else:
            orchestration = run_multi_agent_review(
                hy3_client,
                problem,
                answer,
                provisional,
                mode=review_mode,
            )
            decision = orchestration["decision"]
            hy3_review = decision
            hy3_metadata = {
                "topology": orchestration["topology"],
                "model_calls": orchestration["model_calls"],
                "usage": orchestration["usage_total"],
            }
    final_correct = test_verdict(provisional["execution"], hypothesis)
    process_correct = decision["process_correct"]
    error_types = [item for item in decision["error_types"] if item in ERROR_LABELS]
    return {
        "problem_id": problem["id"],
        "difficulty": problem["difficulty"],
        "final_correct": final_correct,
        "process_correct": process_correct,
        "process_status": decision["process_status"],
        "localization_status": decision["localization_status"],
        "review_coverage": decision["review_coverage"],
        "assessment_note": decision["rationale"],
        "unsupported_correct": unsupported_verdict(final_correct, process_correct),
        "first_error_step": decision["first_error_step"],
        "error_types": error_types,
        "error_labels": [ERROR_LABELS[item] for item in error_types],
        "format_errors": [],
        "criteria": criteria,
        "execution": execution.to_dict(reveal_hidden=reveal_hidden),
        "hypothesis": hypothesis,
        "hy3_review": hy3_review,
        "hy3_review_metadata": hy3_metadata,
        "review_mode": review_mode,
        "orchestration": orchestration,
        "judge_sources": [
            "fixed_tests",
            *(["rubric_rules"] if criteria else []),
            *(["hypothesis"] if hypothesis and hypothesis.get("enabled", True) else []),
            *(
                ["hy3_step_review"]
                if hy3_client is not None and review_mode == "single"
                else []
            ),
            *(
                ["hy3_supervisor_agents"]
                if hy3_client is not None and review_mode == "supervisor"
                else []
            ),
            *(
                ["hy3_bounded_swarm"]
                if hy3_client is not None and review_mode == "swarm"
                else []
            ),
        ],
    }


def _summary_counts(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Report conditional accuracy together with coverage; never coerce None."""
    total = len(records)
    result: dict[str, Any] = {"samples": total}
    for prefix in ("final", "process"):
        values = [item.get(f"{prefix}_correct") for item in records]
        positive = sum(value is True for value in values)
        negative = sum(value is False for value in values)
        assessed = positive + negative
        result.update({
            f"{prefix}_accuracy": positive / assessed if assessed else None,
            f"{prefix}_correct_count": positive,
            f"{prefix}_incorrect_count": negative,
            f"{prefix}_uncertain_count": total - assessed,
            f"{prefix}_evaluated_samples": assessed,
            f"{prefix}_coverage": assessed / total if total else 0.0,
            f"{prefix}_accuracy_ci95": _wilson_interval(positive, assessed),
        })
    unsupported = [unsupported_verdict(item.get("final_correct"), item.get("process_correct")) for item in records]
    known = sum(value is not None for value in unsupported)
    count = sum(value is True for value in unsupported)
    result.update({
        "unsupported_correct_count": count,
        "unsupported_correct_evaluated_samples": known,
        "unsupported_correct_rate": count / known if known else None,
    })
    return result


def _wilson_interval(successes: int, total: int) -> list[float] | None:
    if not total:
        return None
    z = 1.959963984540054
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total)) / denominator
    return [max(0.0, center - margin), min(1.0, center + margin)]


def summarize_results(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_difficulty: dict[str, Any] = {}
    for difficulty in ("easy", "medium", "hard"):
        subset = [item for item in records if item["difficulty"] == difficulty]
        if not subset:
            continue
        by_difficulty[difficulty] = _summary_counts(subset)
    errors = Counter(error for item in records if item.get("process_correct") is False for error in item["error_types"])
    return {
        **_summary_counts(records),
        "error_type_distribution": dict(sorted(errors.items())),
        "by_difficulty": by_difficulty,
        "difficulty_analysis_note": "区间为已判定样本的 Wilson 95% 区间；须同时检查覆盖率、样本独立性与难度校准，不能据此自动断言模型临界点。",
    }


def validate_evaluator(records: list[dict[str, Any]]) -> dict[str, Any]:
    wrong_answer = [item for item in records if not item["ground_truth"]["final_correct"]]
    detected = [item for item in wrong_answer if item["evaluation"]["process_correct"] is False]
    localized = [
        item
        for item in detected
        if type(item["evaluation"]["first_error_step"]) is int
        and item["evaluation"]["first_error_step"] > 0
        and type(item["ground_truth"]["first_error_step"]) is int
        and item["evaluation"]["first_error_step"] == item["ground_truth"]["first_error_step"]
    ]
    correct_answer = [item for item in records if item["ground_truth"]["final_correct"]]
    sound_correct = [item for item in correct_answer if item["ground_truth"]["process_valid"]]
    reviewed_sound = [item for item in sound_correct if type(item["evaluation"]["process_correct"]) is bool]
    false_positives = [item for item in reviewed_sound if item["evaluation"]["process_correct"] is False]
    flagged_correct = [item for item in correct_answer if item["evaluation"]["process_correct"] is False]
    real_issues = [item for item in flagged_correct if not item["ground_truth"]["process_valid"]]
    return {
        "process_uncertain_samples": sum(item["evaluation"]["process_correct"] is None for item in records),
        "wrong_answer_samples": len(wrong_answer),
        "process_problem_detection_rate": len(detected) / len(wrong_answer) if wrong_answer else 0.0,
        "exact_step_localization_accuracy": len(localized) / len(wrong_answer) if wrong_answer else 0.0,
        "answer_correct_samples": len(correct_answer),
        "sound_answer_correct_samples": len(sound_correct),
        "reviewed_sound_answer_correct_samples": len(reviewed_sound),
        "sound_process_review_coverage": len(reviewed_sound) / len(sound_correct) if sound_correct else 0.0,
        "false_positive_count": len(false_positives),
        "false_positive_rate_on_sound_processes": len(false_positives) / len(reviewed_sound) if reviewed_sound else None,
        "flagged_answer_correct_samples": len(flagged_correct),
        "flagged_real_issue_count": len(real_issues),
        "flagged_false_positive_count": len(flagged_correct) - len(real_issues),
        "flagged_real_issue_ratio": len(real_issues) / len(flagged_correct) if flagged_correct else 0.0,
    }
