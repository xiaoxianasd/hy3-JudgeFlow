from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from .hy3_client import Hy3APIError, Hy3Client
from .verdicts import MIN_REVIEW_CONFIDENCE, confidence_value, error_step, localization_status, process_status, test_verdict


ALLOWED_ERROR_TYPES = {
    "problem_misread",
    "concept_error",
    "algorithm_error",
    "theorem_misuse",
    "condition_omission",
    "calculation_error",
    "unjustified_jump",
    "circular_reasoning",
    "complexity_error",
    "hallucination",
    "implementation_error",
    "format_error",
}


@dataclass(frozen=True)
class Specialist:
    name: str
    stage: str
    responsibility: str


SPECIALISTS = (
    Specialist("understanding_agent", "understanding", "检查题意、输入输出、建模假设和条件遗漏"),
    Specialist("algorithm_agent", "algorithm", "检查算法、状态转移、数据结构与定理使用条件"),
    Specialist("proof_agent", "proof", "检查证明完备性、不变量、跳步与循环论证"),
    Specialist("complexity_agent", "complexity", "检查时间和空间复杂度是否与算法及代码一致"),
    Specialist("boundary_agent", "boundary", "检查空输入、重复值、不可达和其他边界条件"),
)


def _confidence(value: Any) -> float:
    return confidence_value(value)


def is_adapter_contract_false_positive(
    problem: dict[str, Any],
    answer: dict[str, Any],
    review: dict[str, Any],
) -> bool:
    """Detect the known source-signature versus solve_case adapter false positive."""
    contract = problem.get("adapter_contract")
    if not isinstance(contract, dict) or contract.get("kind") != "keyword_case_adapter":
        return False
    error_types = review.get("error_types")
    if not isinstance(error_types, list):
        error_types = [review.get("error_type")]
    if {item for item in error_types if item} != {"problem_misread"}:
        return False
    first_step = review.get("first_error_step")
    if not isinstance(first_step, int):
        return False
    review_text = json.dumps(
        {
            "reason": review.get("reason"),
            "rationale": review.get("rationale"),
            "evidence": review.get("evidence"),
            "step_reviews": review.get("step_reviews"),
        },
        ensure_ascii=False,
    ).lower()
    adapter_markers = ("solve_case", "case", "字典")
    source_markers = ("原题", "原函数", "直接接收", "位置参数", "函数签名", "断言", "assert")
    if not any(marker in review_text for marker in adapter_markers):
        return False
    if not any(marker in review_text for marker in source_markers):
        return False
    target_steps = [
        item
        for item in answer.get("reasoning_steps", [])
        if isinstance(item, dict)
        and (item.get("id") == first_step or item.get("stage") == "understanding")
    ]
    answer_text = json.dumps(target_steps, ensure_ascii=False).lower()
    if "case" not in answer_text and "字典" not in answer_text:
        return False
    fields = contract.get("case_fields", [])
    if not isinstance(fields, list):
        return False
    return all(
        re.search(rf"(?<![A-Za-z0-9_]){re.escape(str(field).lower())}(?![A-Za-z0-9_])", answer_text)
        for field in fields
    )


def _normalize_review(spec: Specialist, review: dict[str, Any]) -> dict[str, Any]:
    valid = review.get("valid") if type(review.get("valid")) is bool else None
    error_type = review.get("error_type")
    if error_type not in ALLOWED_ERROR_TYPES:
        error_type = "algorithm_error" if valid is False else None
    step = review.get("first_error_step")
    if type(step) is not int:
        step = None
    raw_evidence = review.get("evidence")
    normalized_evidence = [str(item)[:2000] for item in raw_evidence[:5]] if isinstance(raw_evidence, list) else []
    return {
        "agent": spec.name,
        "stage": spec.stage,
        "status": "completed",
        "valid": valid,
        "reviewed_steps": review.get("reviewed_steps", []),
        "first_error_step": step,
        "error_type": error_type,
        "reason": str(review.get("reason", ""))[:4000],
        "evidence": normalized_evidence,
        "inherited_from_step": review.get("inherited_from_step"),
        "confidence": _confidence(review.get("confidence")),
    }


def _run_specialists(
    client: Hy3Client,
    problem: dict[str, Any],
    answer: dict[str, Any],
    evidence: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reviews: list[dict[str, Any]] = []
    metadata: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=len(SPECIALISTS), thread_name_prefix="hy3-judge") as pool:
        futures = {
            pool.submit(
                client.review_stage,
                problem,
                answer,
                evidence,
                agent_name=spec.name,
                stage=spec.stage,
                responsibility=spec.responsibility,
            ): spec
            for spec in SPECIALISTS
        }
        for future in as_completed(futures):
            spec = futures[future]
            try:
                raw_review, raw_metadata = future.result()
                reviews.append(_normalize_review(spec, raw_review))
                metadata.append({"agent": spec.name, **raw_metadata})
            except Hy3APIError as exc:
                reviews.append(
                    {
                        "agent": spec.name,
                        "stage": spec.stage,
                        "status": "error",
                        "valid": None,
                        "first_error_step": None,
                        "error_type": None,
                        "reason": str(exc),
                        "failure_owner": exc.failure_owner,
                        "evidence": [],
                        "confidence": 0.0,
                    }
                )
    order = {spec.stage: index for index, spec in enumerate(SPECIALISTS)}
    agent_order = {spec.name: index for index, spec in enumerate(SPECIALISTS)}
    reviews.sort(key=lambda item: order[item["stage"]])
    metadata.sort(key=lambda item: agent_order[item["agent"]])
    return reviews, metadata


def _build_decision(
    problem: dict[str, Any],
    answer: dict[str, Any],
    evidence: dict[str, Any],
    reviews: list[dict[str, Any]],
    *,
    specialists: tuple[Specialist, ...] = SPECIALISTS,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    incomplete: list[str] = []
    conclusive: set[str] = set()
    for spec in specialists:
        if not any(item.get("agent") == spec.name for item in reviews):
            incomplete.append(spec.stage)
            conflicts.append({"kind": "agent_missing", "stage": spec.stage})
    if not specialists:
        incomplete.append("semantic_review")
        conflicts.append({"kind": "semantic_review_required"})
    elif not any(spec.stage == "all" for spec in specialists):
        stages = {spec.stage for spec in specialists}
        if any(step.get("stage") not in stages for step in answer.get("reasoning_steps", [])):
            incomplete.append("unassigned_steps")
            conflicts.append({"kind": "unassigned_steps"})

    # All current rubric rules are lexical hints. Even legacy passed/forbidden
    # fields must not become verdicts when re-evaluating stored evidence.
    for review in reviews:
        review["assessment_status"] = "uncertain"
        if review["status"] != "completed":
            incomplete.append(review["stage"])
            conflicts.append(
                {"kind": "agent_unavailable", "stage": review["stage"], "reason": review["reason"]}
            )
            continue
        if is_adapter_contract_false_positive(problem, answer, review):
            # Discarding an objection does not establish that this stage is sound.
            incomplete.append(review["stage"])
            review["ignored_by_supervisor"] = "adapter_contract_false_positive"
            conflicts.append(
                {
                    "kind": "adapter_contract_false_positive",
                    "stage": review["stage"],
                    "agent": review["agent"],
                    "reason": "原函数直接参数与 solve_case(case) 是已声明的等价适配。",
                }
            )
            continue
        confidence = _confidence(review.get("confidence"))
        if type(review.get("valid")) is not bool or confidence < MIN_REVIEW_CONFIDENCE:
            incomplete.append(review["stage"])
            conflicts.append({"kind": "review_inconclusive", "stage": review["stage"]})
            continue
        if not str(review.get("reason") or "").strip():
            incomplete.append(review["stage"])
            conflicts.append({"kind": "review_missing_reason", "stage": review["stage"]})
            continue
        if review["valid"] is False:
            review["assessment_status"] = "invalid"
            inherited = review.get("inherited_from_step")
            step = error_step(answer, inherited if inherited is not None else review.get("first_error_step"))
            candidates.append({
                "step": step,
                "error_type": review["error_type"] or "algorithm_error",
                "source": review["agent"],
                "stage": review["stage"],
                "confidence": confidence,
                "reason": review["reason"],
                "evidence": review["evidence"],
            })
            conclusive.add(review["agent"])
            continue
        target_ids = {
            item["id"] for item in answer.get("reasoning_steps", [])
            if type(item.get("id")) is int
            and (review["stage"] == "all" or item.get("stage") == review["stage"])
        }
        reviewed = review.get("reviewed_steps")
        reviewed_ids = {item for item in reviewed if type(item) is int} if isinstance(reviewed, list) else set()
        if (not target_ids or not target_ids.issubset(reviewed_ids)
                or review.get("first_error_step") is not None or review.get("error_type") is not None):
            incomplete.append(review["stage"])
            conflicts.append({"kind": "review_coverage_or_verdict_conflict", "stage": review["stage"]})
        else:
            review["assessment_status"] = "valid"
            conclusive.add(review["agent"])

    executable_verdict = test_verdict(evidence.get("execution") or {}, evidence.get("hypothesis"))
    if executable_verdict is None:
        incomplete.append("execution")
        conflicts.append({"kind": "execution_inconclusive"})
    if executable_verdict is False:
        candidates.append(
            {
                "step": len(answer.get("reasoning_steps", [])) + 1,
                "error_type": "implementation_error",
                "source": "executable_evidence",
                "confidence": 1.0,
            }
        )

    deterministic_errors = [item for item in candidates if item["source"] == "executable_evidence"]
    coverage = {"expected": len(specialists),
                "completed": sum(item["status"] == "completed" for item in reviews),
                "conclusive": len(conclusive)}
    if not candidates:
        verdict = None if incomplete else True
        return (
            {
                "deterministic_errors": deterministic_errors,
                "incomplete_checks": sorted(set(incomplete)),
                "process_correct": verdict,
                "process_status": process_status(verdict),
                "localization_status": localization_status(verdict, None),
                "review_coverage": coverage,
                "first_error_step": None,
                "error_types": [],
                "confidence": min(
                    [_confidence(item["confidence"]) for item in reviews if item["status"] == "completed"] or [0.0]
                ),
                "rationale": (
                    "评审或验证证据不完整（缺失、低置信度、覆盖不足或意见待复核），无法确认过程成立。"
                    if incomplete else "已完成覆盖全部步骤的语义审查；测试通过不等于完整正确性证明。"
                ),
                "supporting_sources": [],
                "reasoning_evidence": [],
            },
            conflicts,
        )

    # An unlocalized objection may precede every localized candidate.
    first_step = None if any(item["step"] is None for item in candidates) else min(item["step"] for item in candidates)
    first = candidates if first_step is None else [item for item in candidates if item["step"] == first_step]
    error_types = sorted({item["error_type"] for item in first})
    if len(error_types) > 1:
        conflicts.append(
            {"kind": "error_type_disagreement", "step": first_step, "error_types": error_types}
        )
    return (
        {
            "deterministic_errors": deterministic_errors,
            "incomplete_checks": sorted(set(incomplete)),
            "process_correct": False,
            "process_status": "invalid",
            "localization_status": localization_status(False, first_step),
            "review_coverage": coverage,
            "first_error_step": first_step,
            "error_types": error_types,
            "confidence": max(item["confidence"] for item in first),
            "rationale": (
                "已发现过程问题，但尚不能可靠定位首个错误步骤。"
                if first_step is None else f"当前最早定位的错误证据出现在步骤 {first_step}。"
            ),
            "supporting_sources": sorted({item["source"] for item in first}),
            "reasoning_evidence": [
                {
                    "step": item["step"],
                    "error_type": item["error_type"],
                    "source": item["source"],
                    "stage": item.get("stage"),
                    "confidence": item["confidence"],
                    "reason": item.get("reason", ""),
                    "evidence": item.get("evidence", []),
                }
                for item in first if item["source"] != "executable_evidence"
            ],
        },
        conflicts,
    )


def run_single_agent_review(
    client: Hy3Client, problem: dict[str, Any], answer: dict[str, Any], evidence: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    """Apply the same evidence/abstention rules to the single-review baseline."""
    spec = Specialist("hy3_step_review", "all", "完整过程审查")
    metadata = None
    try:
        raw, metadata = client.review(problem, answer, evidence)
        step_reviews = raw.get("step_reviews")
        step_reviews = step_reviews if isinstance(step_reviews, list) else []
        types = raw.get("error_types") or []
        review = _normalize_review(spec, {
            **raw, "valid": raw.get("process_correct"), "reason": raw.get("rationale"),
            "error_type": next((item for item in types if item in ALLOWED_ERROR_TYPES), None),
            "reviewed_steps": [item.get("step") for item in step_reviews
                               if isinstance(item, dict) and type(item.get("valid")) is bool],
        })
        if review["valid"] is True and any(isinstance(item, dict) and item.get("valid") is False for item in step_reviews):
            review["valid"] = None
    except Hy3APIError as exc:
        raw = {"status": "error", "process_correct": None, "failure_owner": exc.failure_owner,
               "error": str(exc)}
        review = {"agent": spec.name, "stage": spec.stage, "status": "error", "valid": None,
                  "confidence": 0.0, "reason": str(exc),
                  "failure_owner": exc.failure_owner}
    decision, conflicts = _build_decision(problem, answer, evidence, [review], specialists=(spec,))
    raw["confidence"] = review["confidence"]
    raw["assessment_status"] = review["assessment_status"]
    if review.get("ignored_by_supervisor"):
        raw["ignored_by_supervisor"] = review["ignored_by_supervisor"]
    decision["conflicts"] = conflicts
    return raw, metadata, decision


def _merge_arbitration(
    decision: dict[str, Any], arbitration: dict[str, Any], answer: dict[str, Any],
    conflicts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Arbitration can add evidence, never turn an incomplete check into a pass."""
    confidence = _confidence(arbitration.get("confidence"))
    arbitration["confidence"] = confidence
    verdict = arbitration.get("process_correct")
    reason = str(arbitration.get("rationale") or "").strip()
    types = arbitration.get("error_types")
    types = [item for item in types if isinstance(item, str) and item in ALLOWED_ERROR_TYPES] if isinstance(types, list) else []
    consistent = verdict is not True or (arbitration.get("first_error_step") is None and not types)
    if type(verdict) is not bool or confidence < MIN_REVIEW_CONFIDENCE or not reason or not consistent:
        conflicts.append({"kind": "arbitration_inconclusive"})
        if decision["process_correct"] is True:
            return {**decision, "process_correct": None, "process_status": "uncertain",
                    "localization_status": "uncertain", "confidence": 0.0,
                    "rationale": "待仲裁意见未获得明确结论，过程证据不足。"}
        return decision
    if verdict is True:
        if decision.get("deterministic_errors"):
            conflicts.append({"kind": "arbitration_cannot_erase_deterministic_evidence",
                              "kept_step": decision["first_error_step"]})
            return decision
        reviewed = arbitration.get("reviewed_steps")
        covered = {x for x in reviewed if type(x) is int} if isinstance(reviewed, list) else set()
        required = {s["id"] for s in answer.get("reasoning_steps", [])}
        if decision.get("incomplete_checks") or not required or not required.issubset(covered):
            conflicts.append({"kind": "arbitration_positive_coverage_incomplete"})
            return decision
        return {**decision, "process_correct": True, "process_status": "valid",
                "localization_status": "not_applicable", "first_error_step": None,
                "error_types": [], "confidence": confidence, "rationale": reason,
                "supporting_sources": ["hy3_arbitration"], "reasoning_evidence": []}
    step = error_step(answer, arbitration.get("first_error_step"))
    current_step = decision.get("first_error_step")
    if step is not None and current_step is not None and step > current_step:
        conflicts.append({"kind": "arbitration_cannot_move_error_later",
                          "kept_step": current_step, "proposed_step": step})
        return decision
    return {**decision, "process_correct": False, "process_status": "invalid",
            "localization_status": localization_status(False, step), "first_error_step": step,
            "error_types": types or decision["error_types"] or ["algorithm_error"],
            "confidence": confidence, "rationale": reason,
            "supporting_sources": arbitration.get("supporting_agents") or [],
            "reasoning_evidence": [{
                "step": step,
                "error_type": (types or decision["error_types"] or ["algorithm_error"])[0],
                "source": "hy3_arbitration",
                "stage": None,
                "confidence": confidence,
                "reason": reason,
                "evidence": [str(item)[:2000] for item in arbitration.get("evidence", [])[:5]]
                if isinstance(arbitration.get("evidence"), list) else [],
            }]}


def run_multi_agent_review(
    client: Hy3Client,
    problem: dict[str, Any],
    answer: dict[str, Any],
    evidence: dict[str, Any],
    *,
    mode: str,
) -> dict[str, Any]:
    if mode not in {"supervisor", "swarm"}:
        raise ValueError(f"Unsupported multi-agent mode: {mode}")
    reviews, metadata = _run_specialists(client, problem, answer, evidence)
    decision, conflicts = _build_decision(problem, answer, evidence, reviews)
    arbitration = None
    arbitration_metadata = None
    should_arbitrate = mode == "swarm" and (
        conflicts or any(item.get("valid") is False for item in reviews)
    )
    if should_arbitrate:
        try:
            arbitration, arbitration_metadata = client.arbitrate_reviews(
                problem, answer, evidence, reviews, conflicts
            )
            decision = _merge_arbitration(decision, arbitration, answer, conflicts)
        except Hy3APIError as exc:
            conflicts.append({"kind": "arbitration_unavailable", "reason": str(exc),
                              "failure_owner": exc.failure_owner})
            if decision["process_correct"] is True:
                decision.update({"process_correct": None, "process_status": "uncertain",
                                 "localization_status": "uncertain",
                                 "rationale": "存在待仲裁意见，但仲裁调用失败，过程结论证据不足。"})

    total_usage: dict[str, int] = {}
    for item in [*metadata, *([arbitration_metadata] if arbitration_metadata else [])]:
        for key, value in item.get("usage", {}).items():
            if isinstance(value, int):
                total_usage[key] = total_usage.get(key, 0) + value
    return {
        "mode": mode,
        "topology": "pipeline+supervisor" if mode == "supervisor" else "pipeline+supervisor+bounded_swarm",
        "specialist_reviews": reviews,
        "conflicts": conflicts,
        "arbitration": arbitration,
        "decision": decision,
        "call_metadata": metadata,
        "arbitration_metadata": arbitration_metadata,
        "usage_total": total_usage,
        "model_calls": len(metadata) + (1 if arbitration_metadata else 0),
    }
