"""Shared three-valued verdicts: missing evidence is not a pass or a failure."""
from __future__ import annotations

import math
from typing import Any


MIN_REVIEW_CONFIDENCE = 0.65
INFRASTRUCTURE_ERRORS = (
    "ReferenceOracleError", "SandboxBackendUnavailable", "SandboxExit", "InvalidSandbox",
    "SandboxProtocolError", "UnsafeLocalSandbox", "UnknownSandboxBackend",
    "Invalid SANDBOX_DOCKER_IMAGE",
)


def confidence_value(value: Any) -> float:
    try:
        number = float(value) if not isinstance(value, bool) else 0.0
        return number if math.isfinite(number) and 0.0 <= number <= 1.0 else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def error_step(answer: dict[str, Any], value: Any) -> int | None:
    steps = answer.get("reasoning_steps", [])
    ids = {step.get("id") for step in steps if type(step.get("id")) is int}
    # The existing protocol reserves the next step for implementation-only faults.
    ids.add(len(steps) + 1)
    return value if type(value) is int and value > 0 and value in ids else None


def process_status(verdict: bool | None) -> str:
    return "valid" if verdict is True else "invalid" if verdict is False else "uncertain"


def localization_status(verdict: bool | None, step: int | None) -> str:
    if verdict is False:
        return "localized" if step is not None else "unlocalized"
    return "not_applicable" if verdict is True else "uncertain"


def is_infrastructure_error(error: Any) -> bool:
    return isinstance(error, str) and error.startswith(INFRASTRUCTURE_ERRORS)


def test_verdict(execution: dict[str, Any], hypothesis: dict[str, Any] | None) -> bool | None:
    """A concrete failure wins; missing or broken checks must never imply success."""
    fixed_unknown = is_infrastructure_error(execution.get("harness_error"))
    fixed = execution.get("all_passed")
    property_result = hypothesis or {}
    property_unknown = property_result.get("status") == "error" or bool(property_result.get("error")) or is_infrastructure_error(
        (property_result.get("counterexample") or {}).get("error")
    )
    property_failed = (
        property_result.get("enabled") is True
        and property_result.get("found") is True
        and not property_unknown
    )
    if (fixed is False and not fixed_unknown) or property_failed:
        return False
    if fixed_unknown or property_unknown or fixed is not True or execution.get("total", 0) <= 0:
        return None
    if hypothesis is not None:
        if type(property_result.get("enabled")) is not bool:
            return None
        if property_result["enabled"] and (
            type(property_result.get("found")) is not bool or property_result.get("examples_checked") == 0
        ):
            return None
    return True


def unsupported_verdict(final: bool | None, process: bool | None) -> bool | None:
    if type(final) is not bool or type(process) is not bool:
        return None
    return final is True and process is False
