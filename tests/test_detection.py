import copy

from hy3_tracejudge.catalog import load_problems
from hy3_tracejudge.detection import summarize_detection
from hy3_tracejudge.executor import run_candidate
from hy3_tracejudge.fixtures import build_labeled_samples
from hy3_tracejudge.cli import build_parser


def test_counts_keep_origins_and_abstentions_separate():
    rows = []
    for origin in ("controlled", "natural"):
        for valid in (True, False):
            for predicted in (True, False, None):
                rows.append({"sample_origin": origin, "ground_truth": {
                    "final_correct": True, "process_valid": valid},
                    "evaluation": {"process_correct": predicted}})
    rows.append({"sample_origin": "natural", "evaluation": {"process_correct": False}})
    groups = summarize_detection(rows)["by_origin"]
    for origin in ("controlled", "natural"):
        counts = groups[origin]["gold_answer_correct"]
        assert [counts[k] for k in ("detected", "missed", "false_positive", "true_negative", "abstained")] == [1, 1, 1, 1, 2]
        assert counts["detected_unlocalized"] == 1
    assert groups["natural"]["all_processes"]["unlabeled"] == 1
    assert groups["controlled"]["all_processes"]["unlabeled"] == 0


def test_counterfactuals_change_one_step_and_reference_code_passes_witnesses():
    problems = {p["id"]: p for p in load_problems()}
    samples = [s for s in build_labeled_samples(list(problems.values()))
               if s["profile"] in ("correct_code_wrong_proof", "condition_overgeneralization")]
    assert len(samples) == 12
    for sample in samples:
        problem = copy.deepcopy(problems[sample["problem_id"]])
        assert sample["answer"]["code"] == problem["reference_solution"]
        changed = [a["id"] for a, b in zip(sample["answer"]["reasoning_steps"], problem["gold_steps"]) if a != b]
        assert changed == [sample["ground_truth"]["first_error_step"]]
        problem["hidden_tests"].append({"name": "claim_witness", **sample["ground_truth"]["counterexample"]})
        assert run_candidate(problem, sample["answer"]["code"]).all_passed, sample["sample_id"]


def test_fixture_profiles_can_be_selected_without_rerunning_other_samples():
    args = build_parser().parse_args([
        "benchmark", "--source", "fixtures",
        "--fixture-profile", "correct_code_wrong_proof",
        "--fixture-profile", "condition_overgeneralization",
    ])
    assert args.fixture_profile == ["correct_code_wrong_proof", "condition_overgeneralization"]


def test_fixture_samples_can_be_selected_for_targeted_retries():
    args = build_parser().parse_args([
        "benchmark", "--source", "fixtures", "--fixture-sample",
        "two_sum_exists-correct_code_wrong_proof", "--fixture-delay", "30",
    ])
    assert args.fixture_sample == ["two_sum_exists-correct_code_wrong_proof"]
    assert args.fixture_delay == 30
