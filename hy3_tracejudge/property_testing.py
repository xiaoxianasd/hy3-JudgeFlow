from __future__ import annotations

import copy
import json
from typing import Any

from hypothesis import HealthCheck, Phase, given, seed, settings, strategies as st

from .executor import run_cases


@st.composite
def _grid_cases(draw: st.DrawFn) -> dict[str, Any]:
    rows = draw(st.integers(min_value=1, max_value=6))
    cols = draw(st.integers(min_value=1, max_value=6))
    cells = draw(
        st.lists(
            st.integers(min_value=0, max_value=1),
            min_size=rows * cols,
            max_size=rows * cols,
        )
    )
    return {"grid": [cells[index * cols : (index + 1) * cols] for index in range(rows)]}


def strategy_for(problem_id: str) -> st.SearchStrategy[dict[str, Any]]:
    small_int = st.integers(min_value=-30, max_value=30)
    strategies: dict[str, st.SearchStrategy[dict[str, Any]]] = {
        "two_sum_exists": st.fixed_dictionaries(
            {
                "nums": st.lists(small_int, min_size=0, max_size=12),
                "target": st.integers(min_value=-60, max_value=60),
            }
        ),
        "bracket_balance": st.fixed_dictionaries(
            {"s": st.text(alphabet="()[]{}", min_size=0, max_size=20)}
        ),
        "max_subarray": st.fixed_dictionaries(
            {"nums": st.lists(small_int, min_size=1, max_size=15)}
        ),
        "grid_shortest_path": _grid_cases(),
        "coin_change": st.fixed_dictionaries(
            {
                "coins": st.lists(
                    st.integers(min_value=1, max_value=15),
                    min_size=1,
                    max_size=6,
                    unique=True,
                ),
                "amount": st.integers(min_value=0, max_value=50),
            }
        ),
        "lis_length": st.fixed_dictionaries(
            {"nums": st.lists(small_int, min_size=0, max_size=15)}
        ),
    }
    probes: dict[str, list[dict[str, Any]]] = {
        "two_sum_exists": [{"nums": [0, 1, 2], "target": 2}, {"nums": [1, 2, 8, 10], "target": 11}],
        "bracket_balance": [{"s": ")("}, {"s": "([)]"}],
        "max_subarray": [{"nums": [-1]}, {"nums": [-3, -1, -2]}],
        "grid_shortest_path": [
            {"grid": [[0, 0, 0], [1, 1, 0], [0, 0, 0]]},
            {"grid": [[1]]},
        ],
        "coin_change": [{"coins": [1, 3, 4], "amount": 6}, {"coins": [4, 6], "amount": 7}],
        "lis_length": [{"nums": [1, 3, 2, 4]}, {"nums": [2, 2]}],
    }
    try:
        # Curated boundary families make short interactive runs stable; the broad
        # strategy still explores combinations that were never hand-written.
        return st.one_of(st.sampled_from(probes[problem_id]), strategies[problem_id])
    except KeyError as exc:
        raise KeyError(f"No Hypothesis strategy for {problem_id}") from exc


def _case_size(value: Any) -> tuple[int, int]:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False)
    magnitude = sum(abs(item) for item in _walk_ints(value))
    return len(encoded), magnitude


def _walk_ints(value: Any) -> list[int]:
    if type(value) is int:
        return [value]
    if isinstance(value, dict):
        return [number for child in value.values() for number in _walk_ints(child)]
    if isinstance(value, list):
        return [number for child in value for number in _walk_ints(child)]
    return []


def find_counterexample(
    problem: dict[str, Any],
    code: str,
    *,
    max_examples: int = 60,
    random_seed: int = 20260824,
) -> dict[str, Any]:
    """Use Hypothesis generation and shrinking to find a differential failure."""
    try:
        strategy = strategy_for(problem["id"])
    except KeyError:
        return {
            "enabled": False,
            "found": False,
            "engine": "none",
            "reason": "external problem without curated Hypothesis strategy",
            "examples_checked": 0,
            "max_examples": max_examples,
            "seed": random_seed,
            "counterexample": None,
        }
    failures: list[dict[str, Any]] = []
    checked = 0

    @seed(random_seed)
    @settings(
        max_examples=max_examples,
        database=None,
        deadline=None,
        report_multiple_bugs=False,
        suppress_health_check=(HealthCheck.too_slow,),
        phases=(Phase.generate, Phase.target, Phase.shrink),
    )
    @given(strategy)
    def differential(case: dict[str, Any]) -> None:
        nonlocal checked
        checked += 1
        result = run_cases(problem, code, [case])
        if result.harness_error:
            failures.append(
                {"case": copy.deepcopy(case), "expected": None, "actual": None, "error": result.harness_error}
            )
            raise AssertionError(result.harness_error)
        test = result.tests[0]
        if not test.passed:
            failures.append(
                {
                    "case": copy.deepcopy(case),
                    "expected": test.expected,
                    "actual": test.actual,
                    "error": test.error,
                }
            )
        assert test.passed

    try:
        differential()
    except AssertionError:
        best = min(failures, key=lambda item: _case_size(item["case"]))
        return {
            "enabled": True,
            "found": True,
            "engine": "hypothesis",
            "examples_checked": checked,
            "max_examples": max_examples,
            "seed": random_seed,
            "counterexample": best,
        }
    return {
        "enabled": True,
        "found": False,
        "engine": "hypothesis",
        "examples_checked": checked,
        "max_examples": max_examples,
        "seed": random_seed,
        "counterexample": None,
    }
