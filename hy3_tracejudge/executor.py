from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, asdict
from typing import Any


RUNNER = r'''
import collections
import heapq
import json
import math
import sys

SAFE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "enumerate": enumerate, "filter": filter, "float": float, "int": int,
    "len": len, "list": list, "map": map, "max": max, "min": min,
    "range": range, "reversed": reversed, "round": round, "set": set,
    "sorted": sorted, "str": str, "sum": sum, "tuple": tuple, "zip": zip,
}

payload = json.loads(sys.stdin.read())
namespace = {
    "__builtins__": SAFE_BUILTINS,
    "collections": collections,
    "heapq": heapq,
    "math": math,
}
try:
    exec(compile(payload["code"], "<candidate>", "exec"), namespace)
    function = namespace[payload["function_name"]]
    results = []
    for test in payload["tests"]:
        try:
            actual = function(test["input"])
            results.append({"name": test["name"], "actual": actual, "error": None})
        except BaseException as exc:
            results.append({"name": test["name"], "actual": None, "error": type(exc).__name__ + ": " + str(exc)})
    print(json.dumps({"ok": True, "results": results}, ensure_ascii=False))
except BaseException as exc:
    print(json.dumps({"ok": False, "error": type(exc).__name__ + ": " + str(exc)}, ensure_ascii=False))
'''


@dataclass
class TestResult:
    name: str
    expected: Any
    actual: Any
    passed: bool
    error: str | None = None
    visibility: str = "hidden"


@dataclass
class ExecutionResult:
    passed: int
    total: int
    all_passed: bool
    tests: list[TestResult]
    harness_error: str | None = None

    def to_dict(self, reveal_hidden: bool = True) -> dict[str, Any]:
        data = asdict(self)
        if not reveal_hidden:
            for test in data["tests"]:
                if test["visibility"] == "hidden":
                    test["expected"] = "<hidden>"
                    test["actual"] = "<hidden>"
        return data


def _equivalent(actual: Any, expected: Any) -> bool:
    if isinstance(expected, float) and isinstance(actual, (int, float)):
        return abs(float(actual) - expected) <= 1e-9 * max(1.0, abs(expected))
    return actual == expected


def run_candidate(
    problem: dict[str, Any],
    code: str,
    *,
    timeout_seconds: float = 3.0,
) -> ExecutionResult:
    tests = []
    for visibility in ("public", "hidden"):
        for test in problem[f"{visibility}_tests"]:
            tests.append({**test, "visibility": visibility})
    payload = {
        "code": code,
        "function_name": problem["function_name"],
        "tests": [{"name": test["name"], "input": test["input"]} for test in tests],
    }
    command = [sys.executable, "-I", "-S", "-c", RUNNER]
    try:
        process = subprocess.run(
            command,
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ExecutionResult(0, len(tests), False, [], "TimeLimitExceeded")
    if process.returncode != 0:
        message = (process.stderr or process.stdout or "runner failed").strip()
        return ExecutionResult(0, len(tests), False, [], f"RunnerExit{process.returncode}: {message}")
    try:
        output = json.loads(process.stdout)
    except json.JSONDecodeError:
        return ExecutionResult(0, len(tests), False, [], f"InvalidRunnerOutput: {process.stdout[:300]}")
    if not output.get("ok"):
        return ExecutionResult(0, len(tests), False, [], output.get("error", "candidate load failed"))
    by_name = {item["name"]: item for item in output["results"]}
    test_results: list[TestResult] = []
    for test in tests:
        item = by_name[test["name"]]
        passed = item["error"] is None and _equivalent(item["actual"], test["expected"])
        test_results.append(
            TestResult(
                name=test["name"],
                expected=test["expected"],
                actual=item["actual"],
                passed=passed,
                error=item["error"],
                visibility=test["visibility"],
            )
        )
    passed_count = sum(item.passed for item in test_results)
    return ExecutionResult(passed_count, len(test_results), passed_count == len(test_results), test_results)


def run_cases(
    problem: dict[str, Any],
    code: str,
    cases: list[dict[str, Any]],
    *,
    timeout_seconds: float = 2.0,
) -> ExecutionResult:
    """Run ad-hoc cases, deriving expected values from the reference solution.

    This is the bridge used by property-based differential testing. Both programs
    run in fresh isolated Python processes; this is a stability boundary, not a
    security-grade sandbox for hostile code.
    """
    named = [
        {"name": f"property_{index}", "input": value, "expected": None}
        for index, value in enumerate(cases, start=1)
    ]
    reference_problem = {
        **problem,
        "public_tests": [],
        "hidden_tests": named,
    }
    reference = run_candidate(
        reference_problem,
        problem["reference_solution"],
        timeout_seconds=timeout_seconds,
    )
    if reference.harness_error:
        return ExecutionResult(
            0,
            len(cases),
            False,
            [],
            "ReferenceOracleError: " + reference.harness_error,
        )
    with_expected = []
    for test, result in zip(named, reference.tests, strict=True):
        with_expected.append({**test, "expected": result.actual})
    candidate_problem = {
        **problem,
        "public_tests": [],
        "hidden_tests": with_expected,
    }
    return run_candidate(candidate_problem, code, timeout_seconds=timeout_seconds)


def reference_check(problem: dict[str, Any]) -> ExecutionResult:
    return run_candidate(problem, problem["reference_solution"])
