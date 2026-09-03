from __future__ import annotations

import json
from collections import Counter
from typing import Any

from .executor import run_candidate
from .hy3_client import Hy3Client
from .multi_agent import is_adapter_contract_false_positive, run_multi_agent_review
from .property_testing import find_counterexample
from .protocol import validate_answer_shape


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
        "passed": passed,
        "reason": reason,
        "error_type": None if passed else criterion["error_type"],
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
            "final_correct": False,
            "process_correct": False,
            "unsupported_correct": False,
            "first_error_step": 1,
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
    failed = [item for item in criteria if not item["passed"]]
    first_error_step: int | None = None
    error_types: list[str] = []
    if failed:
        first_error_step = min(item["source_step"] for item in failed)
        error_types = sorted(
            {item["error_type"] for item in failed if item["source_step"] == first_error_step}
        )
    process_correct = not failed
    property_failed = bool(hypothesis and hypothesis["found"])
    if (not execution.all_passed or property_failed) and process_correct:
        process_correct = False
        first_error_step = len(answer["reasoning_steps"]) + 1
        error_types = ["implementation_error"]
    final_correct = execution.all_passed and not property_failed
    provisional = {
        "criteria": criteria,
        "execution": execution.to_dict(reveal_hidden=True),
        "hypothesis": hypothesis,
    }
    hy3_review = None
    hy3_metadata = None
    orchestration = None
    if hy3_client is not None:
        if review_mode == "single":
            hy3_review, hy3_metadata = hy3_client.review(problem, answer, provisional)
            confidence = float(hy3_review.get("confidence", 0.0) or 0.0)
            llm_step = hy3_review.get("first_error_step")
            if not hy3_review.get("process_correct", True) and confidence >= 0.65:
                if is_adapter_contract_false_positive(problem, answer, hy3_review):
                    hy3_review["ignored_by_supervisor"] = "adapter_contract_false_positive"
                else:
                    process_correct = False
                    if isinstance(llm_step, int) and (
                        first_error_step is None or llm_step < first_error_step
                    ):
                        first_error_step = llm_step
                        proposed = hy3_review.get("error_types", [])
                        error_types = [item for item in proposed if item in ERROR_LABELS] or [
                            "algorithm_error"
                        ]
        else:
            orchestration = run_multi_agent_review(
                hy3_client,
                problem,
                answer,
                provisional,
                mode=review_mode,
            )
            decision = orchestration["decision"]
            process_correct = bool(decision["process_correct"])
            first_error_step = decision.get("first_error_step")
            error_types = [
                item for item in decision.get("error_types", []) if item in ERROR_LABELS
            ]
            if not process_correct and not error_types:
                error_types = ["algorithm_error"]
            hy3_review = decision
            hy3_metadata = {
                "topology": orchestration["topology"],
                "model_calls": orchestration["model_calls"],
                "usage": orchestration["usage_total"],
            }
    unsupported_correct = final_correct and not process_correct
    return {
        "problem_id": problem["id"],
        "difficulty": problem["difficulty"],
        "final_correct": final_correct,
        "process_correct": process_correct,
        "unsupported_correct": unsupported_correct,
        "first_error_step": first_error_step,
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
            "rubric_rules",
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


def summarize_results(records: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(records)
    by_difficulty: dict[str, Any] = {}
    for difficulty in ("easy", "medium", "hard"):
        subset = [item for item in records if item["difficulty"] == difficulty]
        if not subset:
            continue
        by_difficulty[difficulty] = {
            "samples": len(subset),
            "final_accuracy": sum(item["final_correct"] for item in subset) / len(subset),
            "process_accuracy": sum(item["process_correct"] for item in subset) / len(subset),
            "unsupported_correct_rate": sum(item["unsupported_correct"] for item in subset) / len(subset),
        }
    errors = Counter(error for item in records for error in item["error_types"])
    return {
        "samples": total,
        "final_accuracy": sum(item["final_correct"] for item in records) / total if total else 0.0,
        "process_accuracy": sum(item["process_correct"] for item in records) / total if total else 0.0,
        "unsupported_correct_count": sum(item["unsupported_correct"] for item in records),
        "unsupported_correct_rate": sum(item["unsupported_correct"] for item in records) / total if total else 0.0,
        "error_type_distribution": dict(sorted(errors.items())),
        "by_difficulty": by_difficulty,
    }


def validate_evaluator(records: list[dict[str, Any]]) -> dict[str, Any]:
    wrong_answer = [item for item in records if not item["ground_truth"]["final_correct"]]
    detected = [item for item in wrong_answer if not item["evaluation"]["process_correct"]]
    localized = [
        item
        for item in wrong_answer
        if item["evaluation"]["first_error_step"] == item["ground_truth"]["first_error_step"]
    ]
    correct_answer = [item for item in records if item["ground_truth"]["final_correct"]]
    sound_correct = [item for item in correct_answer if item["ground_truth"]["process_valid"]]
    false_positives = [item for item in sound_correct if not item["evaluation"]["process_correct"]]
    flagged_correct = [item for item in correct_answer if not item["evaluation"]["process_correct"]]
    real_issues = [item for item in flagged_correct if not item["ground_truth"]["process_valid"]]
    return {
        "wrong_answer_samples": len(wrong_answer),
        "process_problem_detection_rate": len(detected) / len(wrong_answer) if wrong_answer else 0.0,
        "exact_step_localization_accuracy": len(localized) / len(wrong_answer) if wrong_answer else 0.0,
        "answer_correct_samples": len(correct_answer),
        "sound_answer_correct_samples": len(sound_correct),
        "false_positive_count": len(false_positives),
        "false_positive_rate_on_sound_processes": len(false_positives) / len(sound_correct) if sound_correct else 0.0,
        "flagged_answer_correct_samples": len(flagged_correct),
        "flagged_real_issue_count": len(real_issues),
        "flagged_false_positive_count": len(flagged_correct) - len(real_issues),
        "flagged_real_issue_ratio": len(real_issues) / len(flagged_correct) if flagged_correct else 0.0,
    }
