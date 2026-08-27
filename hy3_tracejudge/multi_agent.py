from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from .hy3_client import Hy3APIError, Hy3Client


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
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return 0.0


def _normalize_review(spec: Specialist, review: dict[str, Any]) -> dict[str, Any]:
    error_type = review.get("error_type")
    if error_type not in ALLOWED_ERROR_TYPES:
        error_type = None if review.get("valid") else "algorithm_error"
    step = review.get("first_error_step")
    if not isinstance(step, int):
        step = None
    return {
        "agent": spec.name,
        "stage": spec.stage,
        "status": "completed",
        "valid": bool(review.get("valid")),
        "reviewed_steps": review.get("reviewed_steps", []),
        "first_error_step": step,
        "error_type": error_type,
        "reason": str(review.get("reason", "")),
        "evidence": review.get("evidence", []),
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
    answer: dict[str, Any],
    evidence: dict[str, Any],
    reviews: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    by_stage = {item["stage"]: item for item in reviews if item["status"] == "completed"}

    for criterion in evidence.get("criteria", []):
        if criterion.get("passed"):
            agent = by_stage.get(criterion["stage"])
            if agent and agent["valid"] is False and agent["confidence"] >= 0.65:
                conflicts.append(
                    {
                        "kind": "agent_vs_rule",
                        "stage": criterion["stage"],
                        "rule": "passed",
                        "agent": "invalid",
                    }
                )
            continue
        agent = by_stage.get(criterion["stage"])
        hard_rule = str(criterion.get("reason", "")).startswith("forbidden:")
        if agent and agent["valid"] is True and agent["confidence"] >= 0.75 and not hard_rule:
            conflicts.append(
                {
                    "kind": "agent_overrides_weak_rule",
                    "stage": criterion["stage"],
                    "rule": criterion.get("reason"),
                    "agent_confidence": agent["confidence"],
                }
            )
            continue
        candidates.append(
            {
                "step": criterion["source_step"],
                "error_type": criterion["error_type"],
                "source": "rubric_rule",
                "confidence": 1.0 if hard_rule else 0.7,
            }
        )

    for review in reviews:
        if review["status"] != "completed":
            conflicts.append(
                {"kind": "agent_unavailable", "stage": review["stage"], "reason": review["reason"]}
            )
            continue
        if review["valid"] is False and review["confidence"] >= 0.65:
            inherited = review.get("inherited_from_step")
            step = inherited if isinstance(inherited, int) else review.get("first_error_step")
            if isinstance(step, int):
                candidates.append(
                    {
                        "step": step,
                        "error_type": review["error_type"] or "algorithm_error",
                        "source": review["agent"],
                        "confidence": review["confidence"],
                    }
                )

    property_failed = bool(evidence.get("hypothesis") and evidence["hypothesis"].get("found"))
    execution_failed = not evidence.get("execution", {}).get("all_passed", False)
    if execution_failed or property_failed:
        candidates.append(
            {
                "step": len(answer.get("reasoning_steps", [])) + 1,
                "error_type": "implementation_error",
                "source": "executable_evidence",
                "confidence": 1.0,
            }
        )

    if not candidates:
        return (
            {
                "process_correct": True,
                "first_error_step": None,
                "error_types": [],
                "confidence": min(
                    [item["confidence"] for item in reviews if item["status"] == "completed"] or [0.0]
                ),
                "rationale": "规则、专业Agent和执行证据均未发现过程错误。",
                "supporting_sources": [],
            },
            conflicts,
        )

    first_step = min(item["step"] for item in candidates)
    first = [item for item in candidates if item["step"] == first_step]
    error_types = sorted({item["error_type"] for item in first})
    if len(error_types) > 1:
        conflicts.append(
            {"kind": "error_type_disagreement", "step": first_step, "error_types": error_types}
        )
    return (
        {
            "process_correct": False,
            "first_error_step": first_step,
            "error_types": error_types,
            "confidence": max(item["confidence"] for item in first),
            "rationale": f"最早错误证据出现在步骤 {first_step}。",
            "supporting_sources": sorted({item["source"] for item in first}),
        },
        conflicts,
    )


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
    decision, conflicts = _build_decision(answer, evidence, reviews)
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
            confidence = _confidence(arbitration.get("confidence"))
            step = arbitration.get("first_error_step")
            current_step = decision.get("first_error_step")
            may_move_earlier = current_step is None or step <= current_step
            if (
                not arbitration.get("process_correct", True)
                and confidence >= 0.65
                and isinstance(step, int)
                and may_move_earlier
            ):
                # Arbitration may move a semantic error earlier, but may not erase
                # a deterministic implementation failure without an earlier cause.
                proposed = [
                    item for item in arbitration.get("error_types", []) if item in ALLOWED_ERROR_TYPES
                ]
                decision = {
                    "process_correct": False,
                    "first_error_step": step,
                    "error_types": proposed or ["algorithm_error"],
                    "confidence": confidence,
                    "rationale": str(arbitration.get("rationale", "Supervisor仲裁确认过程错误。")),
                    "supporting_sources": arbitration.get("supporting_agents", []),
                }
            elif (
                not arbitration.get("process_correct", True)
                and isinstance(step, int)
                and current_step is not None
                and step > current_step
            ):
                conflicts.append(
                    {
                        "kind": "arbitration_cannot_move_error_later",
                        "kept_step": current_step,
                        "proposed_step": step,
                    }
                )
            elif arbitration.get("process_correct") and not decision["process_correct"]:
                conflicts.append(
                    {
                        "kind": "arbitration_cannot_erase_deterministic_evidence",
                        "kept_step": decision["first_error_step"],
                    }
                )
        except Hy3APIError as exc:
            conflicts.append({"kind": "arbitration_unavailable", "reason": str(exc)})

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
