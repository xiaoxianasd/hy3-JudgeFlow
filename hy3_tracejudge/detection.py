"""Gold-conditioned process detection; never pool constructed and natural traces."""
from typing import Any


def detection_counts(records: list[dict[str, Any]]) -> dict[str, Any]:
    counts = dict(samples=len(records), detected=0, missed=0, false_positive=0,
                  true_negative=0, abstained=0, abstained_invalid=0,
                  abstained_valid=0, unlabeled=0, detected_exact_step=0,
                  detected_wrong_step=0, detected_unlocalized=0)
    for record in records:
        gold = record.get("ground_truth", {})
        if type(gold.get("process_valid")) is not bool:
            counts["unlabeled"] += 1
            continue
        valid = gold["process_valid"]
        prediction = record.get("evaluation", {}).get("process_correct")
        if type(prediction) is not bool:
            counts["abstained"] += 1
            counts["abstained_valid" if valid else "abstained_invalid"] += 1
        else:
            key = ("true_negative" if prediction else "false_positive") if valid else (
                "missed" if prediction else "detected")
            counts[key] += 1
            if not valid and prediction is False:
                predicted_step = record.get("evaluation", {}).get("first_error_step")
                gold_step = gold.get("first_error_step")
                if type(predicted_step) is not int:
                    counts["detected_unlocalized"] += 1
                elif type(gold_step) is int and predicted_step == gold_step:
                    counts["detected_exact_step"] += 1
                else:
                    counts["detected_wrong_step"] += 1
    return counts


def summarize_detection(records: list[dict[str, Any]]) -> dict[str, Any]:
    result = {}
    for origin in ("controlled", "natural", "unknown"):
        subset = [r for r in records if r.get("sample_origin", "unknown") == origin]
        result[origin] = {
            "all_processes": detection_counts(subset),
            "gold_answer_correct": detection_counts([
                r for r in subset if r.get("ground_truth", {}).get("final_correct") is True]),
            "by_profile": {profile: detection_counts([r for r in subset if r.get("profile") == profile])
                           for profile in sorted({r["profile"] for r in subset if r.get("profile")})},
        }
    return {"by_origin": result, "definition": (
        "检出=金标过程错且预测错；漏检=金标过程错且预测成立；误报=金标过程成立且预测错；"
        "弃判=有金标但预测未知/缺失（含评审失败）；未标注单列，不计入弃判。"
        "gold_answer_correct按金标答案筛选，不以评估器测试结论筛选；不同来源不合并。"
    )}
