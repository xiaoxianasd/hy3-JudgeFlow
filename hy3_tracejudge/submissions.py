"""Evaluate user-authored code without inventing a reasoning trace for its author."""
from __future__ import annotations

import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError

from .evaluator import ERROR_LABELS
from .executor import run_candidate
from .hy3_client import Hy3APIError, Hy3Client
from .property_testing import find_counterexample
from .sandbox import SandboxLimits
from .verdicts import is_infrastructure_error, test_verdict, unsupported_verdict


class SubmissionSandboxRequired(RuntimeError):
    pass


def require_submission_sandbox() -> None:
    SandboxLimits.from_env(timeout_seconds=3)
    if os.getenv("SANDBOX_BACKEND", "local").strip().lower() != "docker":
        raise SubmissionSandboxRequired("User code requires the Docker sandbox")


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["code", "reasoning"]
    line: StrictInt | None = None
    step: StrictInt | None = None
    error_type: str
    reason: str = Field(min_length=1, max_length=4000)
    suggestion: str = Field(default="", max_length=4000)


class SubmissionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code_correct: StrictBool | None
    process_correct: StrictBool | None
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=8000)
    findings: list[Finding] = Field(default_factory=list, max_length=40)


def normalize_review(raw: dict[str, Any], code: str, steps: list[str]) -> dict[str, Any]:
    try:
        review = SubmissionReview.model_validate(raw).model_dump()
    except ValidationError as exc:
        raise Hy3APIError("Hy3 submission review has an invalid schema") from exc
    findings = []
    rejected = False
    for finding in review["findings"]:
        if finding["error_type"] not in ERROR_LABELS:
            rejected = True
            continue
        if finding["scope"] == "reasoning":
            if not steps:
                # A code-only submission has no author-provided reasoning to grade.
                continue
            if finding["step"] is None or not 1 <= finding["step"] <= len(steps):
                rejected = True
                continue
            finding["line"] = None
        else:
            if finding["line"] is not None and not 1 <= finding["line"] <= len(code.splitlines()):
                rejected = True
                continue
            finding["step"] = None
        findings.append(finding)
    review["findings"] = findings
    if review["confidence"] < 0.65 or rejected:
        review["code_correct"] = None
        review["process_correct"] = None
    else:
        for field, scope in (("code_correct", "code"), ("process_correct", "reasoning")):
            issues = [item for item in findings if item["scope"] == scope]
            if issues:
                review[field] = False
            elif review[field] is False:
                # An unsupported negative verdict is not an error localization.
                review[field] = None
    if not steps:
        review["process_correct"] = None
    review["localization_rejected"] = rejected
    return review


def evaluate_submission(
    problem: dict[str, Any],
    submission: dict[str, Any],
    *,
    hypothesis_examples: int,
    update_phase,
    hy3_client: Hy3Client | None = None,
) -> dict[str, Any]:
    # Repeat the API check at execution time: a queued job may outlive a config change.
    require_submission_sandbox()
    code = submission["code"]
    steps = submission.get("reasoning_steps", [])
    update_phase("code_execution")
    execution = run_candidate(problem, code)
    harness_error = execution.harness_error or ""
    hypothesis = None
    if not harness_error:
        update_phase("property_testing")
        hypothesis = find_counterexample(problem, code, max_examples=hypothesis_examples)
    # A failed oracle/container is not a counterexample to the submitted algorithm.
    final_correct = test_verdict(execution.to_dict(), hypothesis)
    infrastructure_failure = is_infrastructure_error(harness_error) or final_correct is None
    public_execution = execution.to_dict(reveal_hidden=False)
    for item in public_execution["tests"]:
        if item["visibility"] == "hidden" and item.get("error"):
            item["error"] = "<hidden>"
    if infrastructure_failure:
        public_execution["harness_error"] = "沙盒或标准答案验证服务异常，请检查服务后重试。"
        if hypothesis:
            hypothesis = {**hypothesis, "found": False, "counterexample": None, "error": "验证服务异常，属性测试结果不可用"}
    evidence = {
        "execution": public_execution,
        "hypothesis": hypothesis,
        "final_correct": final_correct,
    }
    review = None
    metadata = None
    update_phase("submission_review")
    try:
        raw, raw_metadata = (hy3_client or Hy3Client()).review_submission(problem, submission, evidence)
        review = normalize_review(raw, code, steps)
        metadata = {key: raw_metadata[key] for key in ("model", "latency_ms", "request_id", "usage") if key in raw_metadata}
    except Hy3APIError:
        # Keep executable evidence available when the semantic reviewer is unavailable.
        pass
    code_correct = False if final_correct is False else (review or {}).get("code_correct")
    if infrastructure_failure:
        code_correct = None
    process_correct = (review or {}).get("process_correct") if steps else None
    findings = (review or {}).get("findings", [])
    confirmed = findings if review and review["confidence"] >= 0.65 and not review["localization_rejected"] else []
    first_step = min((item["step"] for item in confirmed if item["scope"] == "reasoning"), default=None)
    first_line = min((item["line"] for item in confirmed if item["scope"] == "code" and item["line"] is not None), default=None)
    errors = {item["error_type"] for item in confirmed}
    if final_correct is False:
        errors.add("implementation_error")
    note = (
        "未提交解题步骤，仅评估代码行为与实现逻辑；不能据此判定作者的推理过程。"
        if not steps else "仅审查用户实际提交的步骤，不补写或猜测作者的推理。"
    )
    if review is None:
        note += " Hy3 审查暂不可用，当前仅保留可执行验证结果，请稍后重试。"
    return {
        "source": "user_submission",
        "problem_id": problem["id"],
        "answer": {
            "code": code,
            "reasoning_steps": [
                {"id": index, "stage": "submitted", "title": f"用户步骤 {index}", "content": content}
                for index, content in enumerate(steps, 1)
            ],
        },
        "generation": None,
        "evaluation": {
            "problem_id": problem["id"], "difficulty": problem["difficulty"],
            "final_correct": final_correct, "code_correct": code_correct,
            "process_correct": process_correct,
            "process_status": "not_provided" if not steps else ("reviewed" if process_correct is not None else "uncertain"),
            "unsupported_correct": unsupported_verdict(final_correct, process_correct),
            "first_error_step": first_step, "first_error_line": first_line,
            "error_types": sorted(errors), "error_labels": [ERROR_LABELS[item] for item in sorted(errors)],
            "execution": public_execution, "hypothesis": hypothesis,
            "submission_review": review, "review_metadata": metadata,
            "review_mode": "submission", "review_available": review is not None,
            "assessment_note": note,
            "judge_sources": ["fixed_tests", *(["hypothesis"] if hypothesis and hypothesis.get("enabled") else []), *(["hy3_submission_review"] if review else [])],
        },
    }
