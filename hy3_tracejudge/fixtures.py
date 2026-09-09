from __future__ import annotations

import copy
import json
from typing import Any


def make_answer(problem: dict[str, Any], profile: str) -> dict[str, Any]:
    steps = copy.deepcopy(problem["gold_steps"])
    code = problem["reference_solution"]
    if profile in {"wrong", "unsupported_correct"}:
        fault = problem["fault"]
        step = next(item for item in steps if item["id"] == fault["step"])
        step["content"] = fault["content"]
        if profile == "wrong":
            code = fault["solution"]
    return {
        "reasoning_steps": steps,
        "complexity": copy.deepcopy(problem["expected_complexity"]),
        "edge_cases": copy.deepcopy(problem["boundary_cases"]),
        "code": code,
        "final_answer": "已给出可执行 solve_case 实现。",
    }


def build_labeled_samples(problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build deduplicated controlled and adversarial evaluator samples.

    Keep the original IDs of retained samples for traceability; the historical
    repeated difficulty mix is removed before adding adversarial variants.
    External problems without controlled labels are not fabricated into gold.
    """
    problems = [problem for problem in problems if problem.get("fault")]
    profiles = {
        "easy": ["gold", "gold", "gold", "wrong"],
        "medium": ["gold", "gold", "unsupported_correct", "wrong"],
        "hard": ["gold", "unsupported_correct", "wrong", "wrong"],
    }
    samples: list[dict[str, Any]] = []
    for problem in problems:
        for index, profile in enumerate(profiles[problem["difficulty"]], start=1):
            process_valid = profile == "gold"
            final_correct = profile != "wrong"
            samples.append(
                {
                    "sample_id": f"{problem['id']}-{index:02d}-{profile}",
                    "problem_id": problem["id"],
                    "difficulty": problem["difficulty"],
                    "profile": profile,
                    "answer": make_answer(problem, profile),
                    "ground_truth": {
                        "final_correct": final_correct,
                        "process_valid": process_valid,
                        "first_error_step": None if process_valid else problem["fault"]["step"],
                        "error_type": None if process_valid else problem["fault"]["error_type"],
                        "annotation_basis": (
                            "reference trace and reference code"
                            if process_valid
                            else "single injected, manually reviewed defect"
                        ),
                    },
                }
            )
    # Repeated identical gold/wrong trajectories must not inflate the denominator.
    unique = {}
    for sample in samples:
        key = (sample["problem_id"], json.dumps(sample["answer"], sort_keys=True, ensure_ascii=False))
        unique.setdefault(key, sample)
    samples = list(unique.values())
    for problem in problems:
        gold = next(s for s in samples if s["problem_id"] == problem["id"] and s["profile"] == "gold")
        keyword = copy.deepcopy(gold)
        keyword.update(sample_id=f"{problem['id']}-keyword-only", profile="keyword_only")
        for step in keyword["answer"]["reasoning_steps"]:
            criterion = next(c for c in problem["rubric"] if c["stage"] == step["stage"])
            step["title"] = "术语堆砌"
            step["content"] = criterion["evidence_any"][0] + "。因为结论成立，所以结论成立，无需推导。"
        keyword["ground_truth"].update(process_valid=False, first_error_step=1,
                                       error_type="circular_reasoning",
                                       annotation_basis="受控变换：保留正确代码，将各步替换为术语及循环论证；非独立人工抽检")
        samples.append(keyword)
        forbidden = next((c for c in problem["rubric"] if c.get("forbidden")), None)
        if forbidden:
            negated = copy.deepcopy(gold)
            negated.update(sample_id=f"{problem['id']}-negated-fault", profile="negated_fault")
            step = next(s for s in negated["answer"]["reasoning_steps"] if s["stage"] == forbidden["stage"])
            step["content"] += f" 错误示例：‘{forbidden['forbidden'][0]}’。该说法不成立，应采用前述正确算法。"
            negated["ground_truth"]["annotation_basis"] = "受控变换：正确过程末尾明确否定错误示例；非独立人工抽检"
            samples.append(negated)
    # Paired counterfactual traces: change only one proof/boundary claim,
    # keeping the reference implementation and all other stages intact.
    cases = {
        "two_sum_exists": ("只要 target-x 等于 x，就证明存在两个不同下标，无需另一个元素。", {"nums": [3], "target": 6}, False,
                           "含重复值时，只要数组长度至少为2，就一定能凑出任意 target。", {"nums": [1, 1], "target": 3}, False),
        "bracket_balance": ("左右括号数量相等是合法嵌套的充分条件，因此计数相等就证明合法。", {"s": ")("}, False,
                            "单一括号类型的数量检查可以直接推广到任意混合括号，顺序不影响合法性。", {"s": "([)]"}, False),
        "max_subarray": ("所有正数总和一定对应一个连续子数组，所以它就是最大连续子数组和。", {"nums": [2, -5, 3]}, 3,
                         "正数数组的最优和非负，因此推广到全负数组时答案也至少为0。", {"nums": [-2]}, -2),
        "grid_shortest_path": ("任意可达网格的最短距离必为 rows+cols-2，因为障碍不影响曼哈顿路径存在。", {"grid": [[0, 0, 0, 0, 0], [1, 1, 1, 1, 0], [0, 0, 0, 0, 0], [0, 1, 1, 1, 1], [0, 0, 0, 0, 0]]}, 16,
                               "无障碍网格能从起点出发，因此起点有障碍时也可将其视为空地继续搜索。", {"grid": [[1]]}, -1),
        "coin_change": ("任何最优方案必须先选最大面值，因为一次减少最多金额必然使总硬币数最少。", {"coins": [1, 3, 4], "amount": 6}, 2,
                        "只有面值1时答案等于amount，因此只要包含面值1，任意币制答案都等于amount。", {"coins": [1, 5], "amount": 10}, 2),
        "lis_length": ("tails中的元素总按原数组下标递增，因此tails本身必是输入的一条子序列。", {"nums": [2, 3, 1]}, 2,
                       "互异元素中非严格递增与严格递增相同，所以含重复值时也可以让相等值延长严格递增子序列。", {"nums": [2, 2]}, 1),
    }
    for problem in problems:
        if problem["id"] not in cases:
            continue
        proof, proof_case, proof_expected, boundary, boundary_case, boundary_expected = cases[problem["id"]]
        for profile, stage, claim, case, expected, error in (
            ("correct_code_wrong_proof", "proof", proof, proof_case, proof_expected, "unjustified_jump"),
            ("condition_overgeneralization", "boundary", boundary, boundary_case, boundary_expected, "condition_omission"),
        ):
            answer = make_answer(problem, "gold")
            step = next(s for s in answer["reasoning_steps"] if s["stage"] == stage)
            step["content"] = claim
            samples.append({"sample_id": f"{problem['id']}-{profile}", "problem_id": problem["id"],
                "difficulty": problem["difficulty"], "profile": profile, "answer": answer,
                "ground_truth": {"final_correct": True, "process_valid": False,
                    "first_error_step": step["id"], "error_type": error,
                    "annotation_basis": "受控单步骤替换，保留参考代码；非独立人工标注",
                    "counterexample": {"input": case, "expected": expected},
                    "false_claim": claim}})
    for sample in samples:
        sample["sample_origin"] = "controlled"
    return samples
