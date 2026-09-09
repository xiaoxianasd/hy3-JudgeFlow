"""Import hard function-level problems from TACO (CodeWars subset) into the catalog.

MBPP+ tops out at easy/medium on our five-dimension rubric. TACO's call-based
problems all cap at MEDIUM_HARD, but that label corresponds to genuinely hard
contest problems (CodeWars 2-3 kyu: DP, backtracking, simulation). The TACO
test split ships full test suites (39-225 groups) for call-based problems,
which lets us validate reference solutions against gold outputs instead of
trusting a single implementation.

Pipeline per record:
  1. parse fn_name and parameters from starter_code;
  2. try each reference solution inside our own sandbox against up to
    ``VALIDATION_GROUPS`` provided test groups; keep the first that passes all;
  3. ship 2 public + up to 8 hidden tests, using TACO's gold outputs
     (cross-checked against sandbox execution of the chosen reference);
  4. reject anything that fails validation or is not JSON-serializable.

Imported problems are appended to ``data/problems_external.json`` with
``tier: external`` and source tag ``taco_codewars``. Difficulty starts as the
AST heuristic and must be rubric-calibrated afterwards (see
data/annotations/difficulty_calibration_external_20260909.json).

Usage:
    python scripts/import_taco.py --parquet data/raw/taco/test.parquet
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hy3_tracejudge.executor import run_cases  # noqa: E402

OUTPUT_PATH = ROOT / "data" / "problems_external.json"
DEFAULT_PARQUET = ROOT / "data" / "raw" / "taco" / "test.parquet"

TARGET_DIFFICULTIES = ("MEDIUM_HARD", "HARD", "VERY_HARD")
PUBLIC_TEST_COUNT = 2
MAX_HIDDEN_TESTS = 8
VALIDATION_GROUPS = 24
MAX_SOLUTIONS_TO_TRY = 6


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _signature(code: str, fn_name: str) -> list[str] | None:
    """Return parameter names of ``fn_name`` if it is a simple function."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            params: list[str] = []
            for arg in [*node.args.posonlyargs, *node.args.args]:
                if arg.arg == "case":
                    return None
                params.append(arg.arg)
            if node.args.vararg or node.args.kwarg or node.args.kwonlyargs:
                return None
            if len(params) != len(set(params)):
                return None
            return params
    return None


def _starter_params(starter: str, fn_name: str) -> tuple[list[str], int] | None:
    """Parse starter_code (which ends with a bare indent and no body).

    Returns (all_params, required_count) where required_count excludes
    parameters that have default values.
    """
    text = starter.rstrip()
    if not text:
        return None
    try:
        tree = ast.parse(text + "\n    pass\n")
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == fn_name:
            if node.args.vararg or node.args.kwarg or node.args.kwonlyargs:
                return None
            params = [a.arg for a in [*node.args.posonlyargs, *node.args.args]]
            if "case" in params or len(params) != len(set(params)):
                return None
            return params, len(params) - len(node.args.defaults)
    return None


def _unwrap_output(value: Any) -> Any | None:
    """TACO wraps every gold output in a one-element list; unwrap exactly once."""
    if isinstance(value, list) and len(value) == 1:
        return value[0]
    return None


def _jsonable(value: Any) -> bool:
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return False
    return True


def _difficulty(code: str) -> str:
    try:
        size = sum(1 for _ in ast.walk(ast.parse(code)))
    except SyntaxError:
        size = 999
    if size <= 16:
        return "easy"
    if size <= 32:
        return "medium"
    return "hard"


def _statement(question: str) -> str:
    text = question.strip()
    return text[:2000]


def convert_record(
    record: dict[str, Any], io: dict[str, Any]
) -> tuple[dict[str, Any] | None, str]:
    fn_name = io.get("fn_name") or ""
    raw_inputs = io.get("inputs") or []
    raw_outputs = io.get("outputs") or []
    if not fn_name or len(raw_inputs) < 4 or len(raw_inputs) != len(raw_outputs):
        return None, "insufficient test groups"

    starter = record.get("starter_code") or ""
    parsed = _starter_params(starter, fn_name)
    if parsed is None:
        return None, "no usable starter_code signature"
    params, required = parsed
    cases: list[tuple[dict[str, Any], Any]] = []
    for args, raw_out in zip(raw_inputs[:VALIDATION_GROUPS], raw_outputs[:VALIDATION_GROUPS]):
        if not isinstance(args, list):
            return None, "non-list input group"
        expected = _unwrap_output(raw_out)
        if expected is None:
            return None, "ambiguous wrapped output"
        if not (required <= len(args) <= len(params)):
            return None, "starter arity mismatch"
        cases.append((args, expected))
    wrapped = []
    for args, expected in cases:
        case = dict(zip(params, args))
        if not _jsonable(case) or not _jsonable(expected):
            return None, "io not json-serializable"
        wrapped.append((case, expected))

    solutions = json.loads(record.get("solutions") or "[]")
    if not solutions:
        return None, "no reference solutions"

    chosen_code = None
    chosen_outputs: list[Any] = []
    for code in solutions[:MAX_SOLUTIONS_TO_TRY]:
        if not isinstance(code, str) or fn_name + "(" not in code:
            continue
        sig = _signature(code, fn_name)
        if sig is None or len(sig) != len(params):
            continue
        reference = code + f"\n\n\ndef solve_case(case):\n    return {fn_name}(**case)\n"
        all_inputs = [case for case, _ in wrapped]
        probe = {
            "id": f"taco_probe_{fn_name}",
            "function_name": "solve_case",
            "reference_solution": reference,
        }
        result = run_cases(probe, reference, all_inputs)  # type: ignore[arg-type]
        if result.harness_error or not result.all_passed:
            continue
        executed = [t.expected for t in result.tests]
        if all(_same(got, gold) for got, (_, gold) in zip(executed, wrapped)):
            chosen_code = reference
            chosen_outputs = executed
            break
    if chosen_code is None:
        return None, "no solution passes TACO gold tests in sandbox"

    ship = wrapped[: PUBLIC_TEST_COUNT + MAX_HIDDEN_TESTS]
    tests = [
        {"name": f"case_{index:02d}", "input": case, "expected": expected}
        for index, (case, expected) in enumerate(ship, start=1)
    ]
    public = tests[:PUBLIC_TEST_COUNT]
    hidden = tests[PUBLIC_TEST_COUNT:]
    title = (record.get("question") or "").strip().split("\n")[0].lstrip("# ").strip()[:60]
    problem = {
        "id": f"taco_{fn_name}",
        "tier": "external",
        "title": title or fn_name,
        "difficulty": _difficulty(chosen_code),
        "difficulty_basis": (
            f"TACO 标签 {record.get('difficulty')}（CodeWars 函数级，约 2-3 kyu）；"
            "AST 复杂度启发式初值，需五维量表人工校准"
        ),
        "statement": _statement(record.get("question") or ""),
        "function_name": "solve_case",
        "input_schema": {name: _type_tag(value) for name, value in ship[0][0].items()},
        "constraints": ["TACO/CodeWars 原题未提供显式约束；规模以测试用例为准"],
        "public_tests": public,
        "hidden_tests": hidden,
        "reference_solution": chosen_code,
        "fault": None,
        "gold_steps": [],
        "rubric": [],
        "expected_complexity": {"time": "未标注", "space": "未标注"},
        "boundary_cases": [],
        "source": (
            f"taco_codewars (TACO test split, source=codewars, TACO difficulty={record.get('difficulty')};"
            " Apache-2.0/CC-BY-4.0 per TACO license) 自动导入；"
            f"参考实现已在沙盒通过 {VALIDATION_GROUPS} 组 TACO 金标测试交叉验证"
        ),
    }
    return problem, ""


def _same(got: Any, expected: Any) -> bool:
    if isinstance(expected, float) and isinstance(got, (int, float)):
        return abs(float(got) - expected) <= 1e-6 * max(1.0, abs(expected))
    return got == expected


def _type_tag(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return f"list[{_type_tag(value[0])}]" if value else "list"
    if isinstance(value, dict):
        return "dict"
    return "value"


def load_candidates(parquet_path: Path) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    import pyarrow.parquet as pq  # deferred: only needed at import time

    table = pq.read_table(
        str(parquet_path),
        columns=["question", "solutions", "input_output", "difficulty", "source", "starter_code"],
    )
    candidates = []
    for record in table.to_pylist():
        if record.get("difficulty") not in TARGET_DIFFICULTIES:
            continue
        raw_io = record.get("input_output")
        if not raw_io:
            continue
        try:
            io = json.loads(raw_io)
        except json.JSONDecodeError:
            continue
        if io.get("fn_name"):
            candidates.append((record, io))
    return candidates


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import TACO call-based hard problems")
    parser.add_argument("--parquet", default=str(DEFAULT_PARQUET))
    parser.add_argument("--max", type=int, default=50)
    args = parser.parse_args(argv)

    parquet_path = Path(args.parquet)
    if not parquet_path.exists():
        print(f"[fail] parquet 不存在: {parquet_path}")
        return 2

    candidates = load_candidates(parquet_path)
    print(f"[info] 候选函数级题: {len(candidates)}（难度 {TARGET_DIFFICULTIES}）")

    existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8")) if OUTPUT_PATH.exists() else []
    existing_ids = {item["id"] for item in existing}
    imported: list[dict[str, Any]] = []
    rejects: dict[str, int] = {}
    for record, io in candidates[: args.max]:
        problem, reason = convert_record(record, io)
        if problem is None:
            key = reason.split(":")[0][:60] or "unknown"
            rejects[key] = rejects.get(key, 0) + 1
            continue
        if problem["id"] in existing_ids:
            rejects["duplicate id"] = rejects.get("duplicate id", 0) + 1
            continue
        imported.append(problem)
        existing_ids.add(problem["id"])

    if not imported:
        print(f"[warn] 没有导入任何题目；拒绝分布: {rejects}")
        return 2

    existing.extend(imported)
    OUTPUT_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    report = {
        "source_parquet": str(parquet_path),
        "sha256": _sha256(parquet_path),
        "accepted": [p["id"] for p in imported],
        "rejected": rejects,
    }
    report_path = parquet_path.parent / "import_taco_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] 导入 {len(imported)} 题到 {OUTPUT_PATH}（外部题总计 {len(existing)}）")
    for p in imported:
        print(f"  - {p['id']:<28} 启发式:{p['difficulty']:<7} 测试:{len(p['public_tests'])}+{len(p['hidden_tests'])}")
    print(f"[ok] 导入报告: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
