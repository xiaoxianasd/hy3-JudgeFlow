from __future__ import annotations

import unittest

from hypothesis import given, settings, strategies as st

from hy3_tracejudge.catalog import get_problem
from hy3_tracejudge.executor import run_cases


def brute_two_sum(case):
    nums, target = case["nums"], case["target"]
    return any(nums[i] + nums[j] == target for i in range(len(nums)) for j in range(i + 1, len(nums)))


class ReferencePropertyTests(unittest.TestCase):
    @settings(max_examples=30, deadline=None)
    @given(
        st.fixed_dictionaries(
            {
                "nums": st.lists(st.integers(-20, 20), max_size=10),
                "target": st.integers(-40, 40),
            }
        )
    )
    def test_two_sum_reference_against_independent_oracle(self, case):
        problem = get_problem("two_sum_exists")
        result = run_cases(problem, problem["reference_solution"], [case])
        self.assertEqual(result.tests[0].actual, brute_two_sum(case))

    @settings(max_examples=25, deadline=None)
    @given(st.lists(st.integers(-50, 50), min_size=1, max_size=12))
    def test_lis_reference_bounds(self, nums):
        problem = get_problem("lis_length")
        result = run_cases(problem, problem["reference_solution"], [{"nums": nums}])
        value = result.tests[0].actual
        self.assertTrue(1 <= value <= len(nums))
        if all(a < b for a, b in zip(nums, nums[1:])):
            self.assertEqual(value, len(nums))


if __name__ == "__main__":
    unittest.main()
