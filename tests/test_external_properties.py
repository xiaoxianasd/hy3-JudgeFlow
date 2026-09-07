import copy
import importlib.util
from pathlib import Path

import pytest
from hypothesis import given, seed, settings, Phase

from hy3_tracejudge.catalog import load_problems, get_problem
from hy3_tracejudge.external_strategies import definitions
from hy3_tracejudge.executor import run_cases, run_candidate
from hy3_tracejudge.property_testing import strategy_for, find_counterexample


EXTERNAL = [p for p in load_problems() if p.get("tier") == "external"]


@pytest.mark.parametrize("problem", EXTERNAL, ids=lambda p: p["id"])
def test_each_external_domain_generates_valid_cases_for_its_reference(problem):
    cases = []
    @seed(20260907)
    @settings(max_examples=12, deadline=None, database=None, phases=[Phase.generate])
    @given(strategy_for(problem["id"]))
    def collect(case):
        assert set(case) == set(problem["input_schema"])
        cases.append(copy.deepcopy(case))
    collect()
    result = run_cases(problem, problem["reference_solution"], cases, timeout_seconds=10)
    assert result.harness_error is None, result.to_dict()
    assert result.all_passed, result.to_dict()


def test_all_current_external_tasks_have_explicit_domains():
    assert {p["id"] for p in EXTERNAL} == set(definitions())


def test_generated_cases_detect_fixed_test_lookup_overfitting():
    problem = get_problem("mbpp_Mbpp/8")
    code = "def solve_case(case):\n"
    for test in problem["public_tests"] + problem["hidden_tests"]:
        code += f"    if case == {test['input']!r}: return {test['expected']!r}\n"
    code += "    return None\n"
    assert run_candidate(problem, code).all_passed
    result = find_counterexample(problem, code, max_examples=20)
    assert result["found"] is True
    assert result["input_domain"]
    assert result["counterexample"]["case"] not in [t["input"] for t in problem["public_tests"] + problem["hidden_tests"]]


def test_intersection_order_is_not_an_error():
    problem = get_problem("mbpp_Mbpp/111")
    code = "def solve_case(case):\n    return sorted(set.intersection(*map(set, case['nestedlist'])), reverse=True)"
    result = run_cases(problem, code, [{"nestedlist": [[1, 2, 3], [3, 2, 1]]}])
    assert result.all_passed


def test_amicable_task_uses_main_function_and_published_expectation():
    problem = get_problem("mbpp_Mbpp/123")
    assert problem["input_schema"] == {"limit": "int"}
    assert problem["public_tests"][0]["expected"] == 504
    assert run_candidate(problem, problem["reference_solution"]).all_passed


def test_importer_resolves_asserted_entry_not_first_helper():
    spec = importlib.util.spec_from_file_location("import_evalplus", Path(__file__).resolve().parents[1] / "scripts/import_evalplus.py")
    importer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(importer)
    record = {"task_id": "test", "text": "Return twice the input.\nassert target(1) == 2",
              "code": "def helper(n):\n    return n+10\ndef target(x):\n    return x*2",
              "base_input": [[1], [2], [3]]}
    result, error = importer.convert_record(record, "test")
    assert not error
    assert result["source_function_name"] == "target"
    assert result["public_tests"][0]["expected"] == 2
    record["text"] = "Return twice the input.\nassert target(1) == 99"
    result, error = importer.convert_record(record, "test")
    assert result is None
    assert "assert expectation" in error
    assert importer._signature(record["code"]) is None
