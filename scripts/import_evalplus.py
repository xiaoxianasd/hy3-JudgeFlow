"""Import EvalPlus (MBPP+ / HumanEval+) problems into the TraceJudge catalog.

MBPP+ is function-level and matches our ``solve_case(case)`` execution harness;
stdin/stdout contest datasets (TACO / CodeContests) are NOT compatible with the
sandbox and are intentionally not imported here.

Pipeline per record:
  1. parse the canonical solution signature and test inputs;
  2. wrap the solution as ``solve_case(case)`` calling the original function;
  3. derive expected outputs by executing the reference inside our sandbox;
  4. reject any record whose reference fails or is not JSON-serializable;
  5. assign a heuristic difficulty from solution complexity (needs review).

Imported problems are written to ``data/problems_external.json`` with
``tier: external``. They participate in ``benchmark --source hy3`` only; the
fixture suite (evaluator validation) stays on the hand-curated seed set.

Usage:
    python scripts/import_evalplus.py                 # mbpp + humaneval
    python scripts/import_evalplus.py --source mbpp --max 120
    python scripts/import_evalplus.py --offline-file path/to/mbpp.jsonl
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hy3_tracejudge.executor import run_cases  # noqa: E402

RAW_DIR = ROOT / "data" / "raw" / "evalplus"
OUTPUT_PATH = ROOT / "data" / "problems_external.json"

DOWNLOAD_MIRRORS = (
    "https://raw.githubusercontent.com/evalplus/evalplus/master/evalplus/data/{name}",
    "https://cdn.jsdelivr.net/gh/evalplus/evalplus@master/evalplus/data/{name}",
    "https://gcore.jsdelivr.net/gh/evalplus/evalplus@master/evalplus/data/{name}",
)

PUBLIC_TEST_COUNT = 2
MAX_HIDDEN_TESTS = 8
DIFFICULTY_THRESHOLDS = {"easy": 16, "medium": 32}  # AST node counts


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_source(name: str, offline_file: str | None) -> Path | None:
    if offline_file:
        path = Path(offline_file)
        return path if path.exists() else None
    target = RAW_DIR / name
    if target.exists() and target.stat().st_size > 1000:
        return target
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    for mirror in DOWNLOAD_MIRRORS:
        url = mirror.format(name=name)
        try:
            with urllib.request.urlopen(url, timeout=90) as response:
                data = response.read()
        except OSError:
            continue
        if len(data) > 1000:
            target.write_bytes(data)
            return target
    return None


def _signature(code: str) -> tuple[str, list[str]] | None:
    """Return (function_name, params) of the first def, if simple."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            params: list[str] = []
            for arg in [*node.args.posonlyargs, *node.args.args]:
                if arg.arg in {"case"}:
                    return None
                params.append(arg.arg)
            if node.args.vararg or node.args.kwarg or node.args.kwonlyargs:
                return None
            if len(params) != len(set(params)):
                return None
            return node.name, params
    return None


def _parse_asserts(test_list: list[str]) -> list[tuple[list[Any], Any]]:
    """Extract (positional args, expected) from MBPP-style assert strings."""
    parsed: list[tuple[list[Any], Any]] = []
    for line in test_list:
        try:
            tree = ast.parse(line, mode="exec")
        except SyntaxError:
            continue
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Assert):
            continue
        test = tree.body[0].test
        if not (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Call)
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.Eq)
            and not test.left.keywords
        ):
            continue
        try:
            args = [ast.literal_eval(arg) for arg in test.left.args]
            expected = ast.literal_eval(test.comparators[0])
        except (ValueError, SyntaxError):
            continue
        parsed.append((args, expected))
    return parsed


def _direct_inputs(record: dict[str, Any]) -> list[tuple[list[Any], Any | None]] | None:
    """Handle evalplus plus-format records (base_input / plus_input)."""
    base = record.get("base_input")
    plus = record.get("plus_input")
    if not isinstance(base, list):
        return None
    cases = [(_normalize_args(item), None) for item in base]
    if isinstance(plus, list):
        cases.extend((_normalize_args(item), None) for item in plus)
    return cases


def _normalize_args(item: Any) -> list[Any]:
    if isinstance(item, list):
        return item
    if item is None:
        return []
    return [item]


def _text_and_code(record: dict[str, Any]) -> tuple[str, str] | None:
    text = record.get("text") or record.get("prompt") or ""
    code = record.get("code") or record.get("canonical_solution") or ""
    if not text or not code:
        return None
    return text.strip(), code.strip()


def _statement(text: str) -> str:
    """MBPP prompts embed a def line + docstring; keep prose only."""
    match = _DEF_LINE.search(text)
    if match and match.start() > 0:
        return text[: match.start()].strip()
    return text


import re  # noqa: E402

_DEF_LINE = re.compile(r"^def\s+\w+\s*\(", re.MULTILINE)


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
    if isinstance(value, tuple):
        return "tuple"
    if isinstance(value, dict):
        return "dict"
    return "value"


def _difficulty(code: str) -> str:
    try:
        size = sum(1 for _ in ast.walk(ast.parse(code)))
    except SyntaxError:
        size = 999
    if size <= DIFFICULTY_THRESHOLDS["easy"]:
        return "easy"
    if size <= DIFFICULTY_THRESHOLDS["medium"]:
        return "medium"
    return "hard"


def _jsonable(value: Any) -> bool:
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return False
    return True


def convert_record(record: dict[str, Any], source_tag: str) -> tuple[dict[str, Any] | None, str]:
    """Convert one upstream record, validating it inside our own sandbox."""
    parsed = _text_and_code(record)
    if not parsed:
        return None, "missing text/code"
    text, code = parsed
    signature = _signature(code)
    if not signature:
        return None, "unsupported signature"
    function_name, params = signature
    if not params:
        return None, "no parameters"
    if any(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.parse(code).body):
        return None, "solution contains import statements"

    if isinstance(record.get("test_list"), list) and record["test_list"]:
        cases = _parse_asserts(record["test_list"])
        if not cases:
            return None, "no parseable asserts"
    else:
        cases = _direct_inputs(record)
        if not cases:
            return None, "no usable inputs"

    if len(params) != len({len(args) for args, _ in cases} - {0}) and not all(
        len(args) == len(params) for args, _ in cases
    ):
        return None, "assert arity mismatch"

    wrapped_cases = []
    for args, expected in cases:
        if len(args) != len(params):
            return None, "assert arity mismatch"
        case = dict(zip(params, args, strict=True))
        if not _jsonable(case):
            return None, "input not json-serializable"
        wrapped_cases.append((case, expected))
    if len(wrapped_cases) < 3:
        return None, "too few tests"

    statement = _statement(text)
    if len(statement) < 12:
        return None, "statement too short"
    reference = code + f"\n\n\ndef solve_case(case):\n    return {function_name}(**case)\n"
    placeholder = {
        "id": f"{source_tag}_{record.get('task_id', record.get('task_id_hash', 'x'))}",
        "function_name": "solve_case",
        "reference_solution": reference,
    }
    all_inputs = [case for case, _ in wrapped_cases]
    result = run_cases(placeholder, reference, all_inputs)  # type: ignore[arg-type]
    if result.harness_error:
        return None, f"sandbox incompatible: {result.harness_error[:80]}"
    if not result.all_passed:
        return None, "reference nondeterministic or self-inconsistent"

    tests: list[dict[str, Any]] = []
    for index, (case, assert_expected) in enumerate(wrapped_cases, start=1):
        derived = result.tests[index - 1].expected
        if derived is None:
            return None, "reference returned None"
        if not _jsonable(derived):
            return None, "output not json-serializable"
        if assert_expected is not None and not _same(derived, assert_expected):
            return None, "reference disagrees with assert expectation"
        tests.append(
            {
                "name": f"case_{index:02d}",
                "input": case,
                "expected": derived,
            }
        )

    public = tests[:PUBLIC_TEST_COUNT]
    hidden = tests[PUBLIC_TEST_COUNT : PUBLIC_TEST_COUNT + MAX_HIDDEN_TESTS]
    first_case = all_inputs[0]
    problem = {
        "id": placeholder["id"],
        "tier": "external",
        "title": statement.split("\n")[0][:40],
        "difficulty": _difficulty(code),
        "difficulty_basis": f"AST 复杂度启发式（≤{DIFFICULTY_THRESHOLDS['easy']} easy / ≤{DIFFICULTY_THRESHOLDS['medium']} medium / 其余 hard），需人工复核",
        "statement": statement,
        "function_name": "solve_case",
        "input_schema": {name: _type_tag(value) for name, value in first_case.items()},
        "constraints": ["外部导入题（MBPP+/HumanEval+）未提供显式约束；规模以测试用例为准"],
        "public_tests": public,
        "hidden_tests": hidden,
        "reference_solution": reference,
        "fault": None,
        "gold_steps": [],
        "rubric": [],
        "expected_complexity": {"time": "未标注", "space": "未标注"},
        "boundary_cases": [],
        "source": f"{source_tag} (EvalPlus, Apache-2.0) 自动导入；原始测试与参考实现已沙盒验证",
    }
    return problem, ""


def _same(derived: Any, expected: Any) -> bool:
    if isinstance(expected, float) and isinstance(derived, (int, float)):
        return abs(float(derived) - expected) <= 1e-6 * max(1.0, abs(expected))
    return derived == expected


def import_file(path: Path, source_tag: str, max_problems: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
    problems: list[dict[str, Any]] = []
    rejects: dict[str, int] = {}
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if len(problems) >= max_problems:
                break
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                rejects["unparsable_line"] = rejects.get("unparsable_line", 0) + 1
                continue
            problem, reason = convert_record(record, source_tag)
            if problem is None:
                key = reason.split(":")[0][:60]
                rejects[key] = rejects.get(key, 0) + 1
            else:
                problems.append(problem)
    return problems, rejects


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import EvalPlus problems into TraceJudge")
    parser.add_argument("--source", choices=("mbpp", "humaneval", "both"), default="both")
    parser.add_argument("--max", type=int, default=400, help="每源最多导入题数")
    parser.add_argument("--offline-file", help="使用本地已下载的 jsonl 文件（如 mbpp.jsonl）")
    args = parser.parse_args(argv)

    names = ["mbpp.jsonl", "humaneval.jsonl"] if args.source == "both" else [f"{args.source}.jsonl"]
    existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8")) if OUTPUT_PATH.exists() else []
    existing_ids = {item["id"] for item in existing}
    imported: list[dict[str, Any]] = []
    report: dict[str, Any] = {"sources": {}}

    for name in names:
        path = ensure_source(name, args.offline_file)
        if path is None:
            print(f"[skip] 无法获取 {name}（网络不可达且无本地文件）；请开启代理后重试")
            continue
        source_tag = "mbpp" if name.startswith("mbpp") else "humaneval"
        problems, rejects = import_file(path, source_tag, args.max)
        fresh = [p for p in problems if p["id"] not in existing_ids]
        imported.extend(fresh)
        report["sources"][name] = {
            "path": str(path),
            "sha256": _sha256(path),
            "accepted": len(fresh),
            "rejected": rejects,
            "difficulty": {
                level: sum(1 for p in fresh if p["difficulty"] == level)
                for level in ("easy", "medium", "hard")
            },
        }
        print(f"[done] {name}: 导入 {len(fresh)} 题，拒绝 {sum(rejects.values())} 条")

    if not imported:
        print("[warn] 没有导入任何题目；data/problems_external.json 未修改")
        return 2

    existing.extend(imported)
    OUTPUT_PATH.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report_path = RAW_DIR / "import_report.json"
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] 写入 {len(imported)} 题到 {OUTPUT_PATH}（总计 {len(existing)} 题）")
    print(f"[ok] 导入报告: {report_path}")
    print("[note] 外部题 tier=external：进入 hy3 基准与 fixtures 自动跳过；rubric/gold_steps 为空，过程判定依赖执行证据与 Hy3 Agent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
