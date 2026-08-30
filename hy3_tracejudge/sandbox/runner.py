from __future__ import annotations

import ast
import bisect as _bisect
import collections as _collections
import contextlib
import functools as _functools
import heapq as _heapq
import io
import json
import math as _math
import sys
from typing import Any


HARD_MAX_INPUT_BYTES = 8 * 1024 * 1024
_BANNED_NAMES = {
    "__builtins__",
    "__import__",
    "breakpoint",
    "compile",
    "delattr",
    "dir",
    "eval",
    "exec",
    "getattr",
    "globals",
    "help",
    "input",
    "locals",
    "memoryview",
    "open",
    "setattr",
    "type",
    "vars",
}


class SecurityPolicyError(ValueError):
    pass


class _SecurityVisitor(ast.NodeVisitor):
    def __init__(self, max_nodes: int = 5000):
        self.max_nodes = max_nodes
        self.nodes = 0

    def generic_visit(self, node: ast.AST) -> None:
        self.nodes += 1
        if self.nodes > self.max_nodes:
            raise SecurityPolicyError("ASTNodeLimitExceeded")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise SecurityPolicyError("ImportNotAllowed")
        if isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith)):
            raise SecurityPolicyError(f"{type(node).__name__}NotAllowed")
        super().generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        if node.attr.startswith("_"):
            raise SecurityPolicyError("PrivateAttributeNotAllowed")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if node.id.startswith("__") or node.id in _BANNED_NAMES:
            raise SecurityPolicyError(f"NameNotAllowed:{node.id}")
        self.generic_visit(node)


class _ModuleFacade:
    __slots__ = ("_allowed",)

    def __init__(self, **allowed: Any):
        object.__setattr__(self, "_allowed", allowed)

    def __getattribute__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        allowed = object.__getattribute__(self, "_allowed")
        try:
            return allowed[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class _BoundedWriter(io.TextIOBase):
    def __init__(self, limit: int):
        self.limit = limit
        self.written = 0

    def write(self, value: str) -> int:
        self.written += len(value.encode("utf-8", errors="replace"))
        if self.written > self.limit:
            raise RuntimeError("CandidateOutputLimitExceeded")
        return len(value)

    def flush(self) -> None:
        return None


SAFE_BUILTINS = {
    "Exception": Exception,
    "IndexError": IndexError,
    "KeyError": KeyError,
    "RuntimeError": RuntimeError,
    "TypeError": TypeError,
    "ValueError": ValueError,
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "divmod": divmod,
    "enumerate": enumerate,
    "filter": filter,
    "float": float,
    "int": int,
    "isinstance": isinstance,
    "iter": iter,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "next": next,
    "pow": pow,
    "range": range,
    "reversed": reversed,
    "round": round,
    "set": set,
    "slice": slice,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
}


SAFE_MODULES = {
    "bisect": _ModuleFacade(
        bisect_left=_bisect.bisect_left,
        bisect_right=_bisect.bisect_right,
        insort_left=_bisect.insort_left,
        insort_right=_bisect.insort_right,
    ),
    "collections": _ModuleFacade(
        Counter=_collections.Counter,
        defaultdict=_collections.defaultdict,
        deque=_collections.deque,
    ),
    "functools": _ModuleFacade(cache=_functools.cache, lru_cache=_functools.lru_cache),
    "heapq": _ModuleFacade(
        heapify=_heapq.heapify,
        heappop=_heapq.heappop,
        heappush=_heapq.heappush,
        heappushpop=_heapq.heappushpop,
        heapreplace=_heapq.heapreplace,
        nlargest=_heapq.nlargest,
        nsmallest=_heapq.nsmallest,
    ),
    "math": _ModuleFacade(
        ceil=_math.ceil,
        comb=_math.comb,
        cos=_math.cos,
        e=_math.e,
        exp=_math.exp,
        factorial=_math.factorial,
        floor=_math.floor,
        gcd=_math.gcd,
        inf=_math.inf,
        isclose=_math.isclose,
        isqrt=_math.isqrt,
        lcm=_math.lcm,
        log=_math.log,
        pi=_math.pi,
        prod=_math.prod,
        sin=_math.sin,
        sqrt=_math.sqrt,
    ),
}


def _apply_os_limits(limits: dict[str, Any]) -> None:
    try:
        import resource
    except ImportError:
        return
    memory_bytes = int(limits["memory_mb"]) * 1024 * 1024
    cpu_seconds = max(1, int(float(limits["timeout_seconds"]) + 0.999))
    for resource_id, value in (
        (resource.RLIMIT_CORE, 0),
        (resource.RLIMIT_FSIZE, int(limits["max_output_bytes"])),
        (resource.RLIMIT_NOFILE, 64),
        (resource.RLIMIT_AS, memory_bytes),
        (resource.RLIMIT_CPU, cpu_seconds),
    ):
        try:
            resource.setrlimit(resource_id, (value, value))
        except (OSError, ValueError):
            pass


def _read_payload() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(HARD_MAX_INPUT_BYTES + 1)
    if len(raw) > HARD_MAX_INPUT_BYTES:
        raise ValueError("SandboxInputLimitExceeded")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("SandboxPayloadMustBeObject")
    return payload


def _limits(payload: dict[str, Any]) -> dict[str, Any]:
    supplied = payload.get("limits", {})
    if not isinstance(supplied, dict):
        raise ValueError("InvalidSandboxLimits")
    return {
        "timeout_seconds": max(0.1, min(float(supplied.get("timeout_seconds", 3.0)), 30.0)),
        "memory_mb": max(64, min(int(supplied.get("memory_mb", 256)), 2048)),
        "max_output_bytes": max(
            64 * 1024, min(int(supplied.get("max_output_bytes", 1024 * 1024)), 8 * 1024 * 1024)
        ),
        "max_code_bytes": max(
            1024, min(int(supplied.get("max_code_bytes", 64 * 1024)), 256 * 1024)
        ),
        "max_tests": max(1, min(int(supplied.get("max_tests", 512)), 2000)),
        "max_value_bytes": max(
            1024, min(int(supplied.get("max_value_bytes", 64 * 1024)), 1024 * 1024)
        ),
    }


def _validate_code(code: Any, max_code_bytes: int) -> ast.Module:
    if not isinstance(code, str):
        raise ValueError("CodeMustBeString")
    if len(code.encode("utf-8")) > max_code_bytes:
        raise ValueError("CodeSizeLimitExceeded")
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise ValueError(f"SyntaxError:{exc.msg}") from exc
    _SecurityVisitor().visit(tree)
    return tree


def _execute(payload: dict[str, Any]) -> dict[str, Any]:
    limits = _limits(payload)
    _apply_os_limits(limits)
    tests = payload.get("tests")
    if not isinstance(tests, list) or len(tests) > limits["max_tests"]:
        raise ValueError("TestCountLimitExceeded")
    function_name = payload.get("function_name")
    if not isinstance(function_name, str) or not function_name.isidentifier():
        raise ValueError("InvalidFunctionName")
    tree = _validate_code(payload.get("code"), limits["max_code_bytes"])
    namespace: dict[str, Any] = {"__builtins__": SAFE_BUILTINS, **SAFE_MODULES}
    output_sink = _BoundedWriter(limits["max_output_bytes"])
    with contextlib.redirect_stdout(output_sink), contextlib.redirect_stderr(output_sink):
        exec(compile(tree, "<candidate>", "exec"), namespace)
        function = namespace.get(function_name)
        if not callable(function):
            raise ValueError(f"MissingFunction:{function_name}")
        results: list[dict[str, Any]] = []
        result_bytes = 0
        for test in tests:
            if not isinstance(test, dict) or "name" not in test or "input" not in test:
                raise ValueError("InvalidTestRecord")
            try:
                actual = function(test["input"])
                encoded_actual = json.dumps(actual, ensure_ascii=False, allow_nan=False).encode("utf-8")
                if len(encoded_actual) > limits["max_value_bytes"]:
                    raise RuntimeError("ReturnValueLimitExceeded")
                item = {"name": str(test["name"]), "actual": actual, "error": None}
            except BaseException as exc:
                message = (type(exc).__name__ + ": " + str(exc))[:300]
                item = {"name": str(test["name"]), "actual": None, "error": message}
            result_bytes += len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
            if result_bytes > limits["max_output_bytes"]:
                raise RuntimeError("SandboxOutputLimitExceeded")
            results.append(item)
    return {"ok": True, "results": results}


def main() -> int:
    try:
        payload = _read_payload()
        output = _execute(payload)
    except BaseException as exc:
        output = {"ok": False, "error": (type(exc).__name__ + ": " + str(exc))[:500]}
    encoded = json.dumps(output, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
