from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .catalog import ROOT, get_problem, load_problems
from .evaluator import evaluate_answer, summarize_results, validate_evaluator
from .fixtures import build_labeled_samples
from .hy3_client import Hy3APIError, Hy3Client
from .reporting import write_json, write_jsonl, write_results_csv
from .auditing import build_audit_queue, summarize_audits
from .detection import summarize_detection


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _load_answer(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if "answer" in value and isinstance(value["answer"], dict):
        return value["answer"]
    return value


def command_doctor(_: argparse.Namespace) -> int:
    client = Hy3Client()
    try:
        _print(client.health())
        return 0
    except Hy3APIError as exc:
        _print({"ok": False, "error": str(exc), "endpoint": client.config.base_url})
        return 2


def command_list(_: argparse.Namespace) -> int:
    _print(
        [
            {
                "id": item["id"],
                "title": item["title"],
                "difficulty": item["difficulty"],
                "source": item["source"],
            }
            for item in load_problems()
        ]
    )
    return 0


def command_solve(args: argparse.Namespace) -> int:
    problem = get_problem(args.problem)
    client = Hy3Client()
    answer, generation = client.solve(problem)
    evaluation = evaluate_answer(
        problem,
        answer,
        hypothesis_examples=args.hypothesis_examples,
        hy3_client=None if args.no_hy3_review else client,
        review_mode=args.review_mode,
    )
    record = {
        "run_type": "hy3_model_evaluation",
        "problem": {key: problem[key] for key in ("id", "title", "difficulty", "statement")},
        "answer": answer,
        "generation_metadata": generation,
        "evaluation": evaluation,
    }
    if args.output:
        write_json(args.output, record)
    _print(record)
    return 0


def command_evaluate(args: argparse.Namespace) -> int:
    problem = get_problem(args.problem)
    answer = _load_answer(args.answer)
    evaluation = evaluate_answer(
        problem,
        answer,
        hypothesis_examples=args.hypothesis_examples,
        hy3_client=Hy3Client() if args.hy3_review else None,
        review_mode=args.review_mode,
    )
    if args.output:
        write_json(args.output, evaluation)
    _print(evaluation)
    return 0


def _fixture_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    samples = build_labeled_samples(load_problems())
    if args.fixture_profile:
        selected_profiles = set(args.fixture_profile)
        samples = [sample for sample in samples if sample["profile"] in selected_profiles]
    if args.fixture_sample:
        selected_samples = set(args.fixture_sample)
        samples = [sample for sample in samples if sample["sample_id"] in selected_samples]
    if not samples:
        raise ValueError("构造样本筛选条件没有选中任何样本")
    client = Hy3Client() if args.hy3_review else None
    records = []
    for sample in samples:
        print(f"[{len(records)+1}/{len(samples)}] {sample['sample_id']}", file=sys.stderr, flush=True)
        evaluation = evaluate_answer(
            get_problem(sample["problem_id"]),
            sample["answer"],
            hypothesis_examples=args.hypothesis_examples,
            hy3_client=client,
            review_mode=args.review_mode,
        )
        records.append({**sample, "problem": _problem_snapshot(get_problem(sample["problem_id"])),
                        "evaluation": evaluation})
        if args.output:
            write_json(args.output.with_suffix(".partial.json"), {
                "run_type": "evaluator_validation_fixtures", "status": "in_progress",
                "semantic_review_enabled": client is not None, "review_mode": args.review_mode,
                "records": records, "process_detection": summarize_detection(records)})
        if args.fixture_delay and len(records) < len(samples):
            time.sleep(args.fixture_delay)
    evaluations = [item["evaluation"] for item in records]
    return {
        "run_type": "evaluator_validation_fixtures",
        "disclaimer": "去重受控轨迹与对抗变换，仅验证评估器；不是 Hy3 解题能力结果或独立人工抽检。",
        "semantic_review_enabled": client is not None,
        "review_mode": args.review_mode,
        "hypothesis_examples": args.hypothesis_examples,
        "fixture_profiles": sorted(set(args.fixture_profile or [])),
        "fixture_samples": sorted(set(args.fixture_sample or [])),
        "summary": summarize_results(evaluations),
        "validity": validate_evaluator(records),
        "process_detection": summarize_detection(records),
        "records": records,
    }


def _filter_tier(problems: list[dict[str, Any]], tier: str) -> list[dict[str, Any]]:
    if tier == "all":
        return problems
    return [problem for problem in problems if problem.get("tier", "seed") == tier]


def _problem_snapshot(problem: dict[str, Any]) -> dict[str, Any]:
    return {key: problem.get(key) for key in (
        "id", "title", "statement", "difficulty", "difficulty_basis", "source",
        "input_schema", "constraints", "public_tests", "adapter_contract",
    )}


def _select_benchmark_problems(problems: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    selected = _filter_tier(problems, args.tier)
    if args.per_difficulty:
        buckets = [[p for p in selected if p["difficulty"] == level]
                   for level in ("easy", "medium", "hard")]
        if any(len(bucket) < args.per_difficulty for bucket in buckets):
            raise ValueError("所选题集不能满足每个难度的题数")
        selected = [p for group in zip(*(b[:args.per_difficulty] for b in buckets)) for p in group]
    return selected[:args.limit] if args.limit else selected


def _hy3_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    from dataclasses import asdict
    from .benchmarking import fingerprint, implementation_fingerprint, run_checkpointed
    import hypothesis
    client = Hy3Client()
    selected = _select_benchmark_problems(load_problems(), args)
    if not selected:
        raise ValueError("所选题集为空")
    model_config = {key: value for key, value in asdict(client.config).items() if key != "api_key"}
    manifest = {
        "settings": {
            "model": client.config.model, "endpoint": client.config.base_url,
            "review_mode": args.review_mode, "tier": args.tier,
            "hypothesis_examples": args.hypothesis_examples,
            "selected_problem_ids": [p["id"] for p in selected],
            "difficulty_note": "外部题难度为未人工校准的 AST 启发式；分层结果不可直接视为模型能力临界点。",
        },
        "model_config": model_config, "problem_sha256": fingerprint(selected),
        "implementation_sha256": implementation_fingerprint(),
        "python": list(sys.version_info[:3]), "hypothesis_version": hypothesis.__version__,
    }
    records = [{"sample_id": f"hy3-{p['id']}", "problem_id": p["id"], "difficulty": p["difficulty"],
                "sample_origin": "natural", "problem": _problem_snapshot(p), "status": "pending", "attempts": 0} for p in selected]
    return run_checkpointed(
        output=args.output or ROOT / "reports" / "hy3_benchmark.json", manifest=manifest,
        initial_records=records, solve=lambda pid: client.solve(get_problem(pid)),
        evaluate=lambda pid, answer: evaluate_answer(get_problem(pid), answer,
            hypothesis_examples=args.hypothesis_examples, hy3_client=client, review_mode=args.review_mode),
        resume=args.resume, retry_failed=args.retry_failed, max_attempts=args.max_attempts,
        retry_delay=args.retry_delay,
    )


def command_benchmark(args: argparse.Namespace) -> int:
    if args.source == "fixtures" and (args.limit or args.per_difficulty or args.tier != "all"):
        raise ValueError("--limit、--per-difficulty 和 --tier 仅用于 --source hy3")
    if args.retry_failed and not args.resume:
        raise ValueError("--retry-failed 必须与 --resume 一起使用")
    if args.source == "fixtures" and (args.resume or args.retry_failed):
        raise ValueError("断点续跑用于 --source hy3；构造集请输出到新文件")
    if args.source != "fixtures" and (args.fixture_profile or args.fixture_sample or args.fixture_delay):
        raise ValueError("--fixture-profile/--fixture-sample/--fixture-delay 仅用于 --source fixtures")
    if args.output and args.output.suffix.lower() != ".json":
        raise ValueError("--output 必须是 .json 文件，旁边自动生成 .jsonl/.csv")
    result = _fixture_benchmark(args) if args.source == "fixtures" else _hy3_benchmark(args)
    output = args.output or ROOT / "reports" / f"{args.source}_benchmark.json"
    records = result["records"]
    if args.source == "fixtures":
        write_json(output, result)
        write_jsonl(output.with_suffix(".jsonl"), records)
        write_results_csv(output.with_suffix(".csv"), records)
        partial = output.with_suffix(".partial.json")
        if partial.exists():
            partial.unlink()
    if args.source == "fixtures":
        audits = []
        for item in records:
            if not item["ground_truth"]["final_correct"]:
                continue
            evaluation = item["evaluation"]
            audits.append(
                {
                    "sample_id": item["sample_id"],
                    "problem_id": item["problem_id"],
                    "annotator": "fixture_gold_annotation",
                    "independent_human_audit": False,
                    "final_answer_correct": True,
                    "process_issue_flagged": evaluation["process_correct"] is False if evaluation["process_correct"] is not None else None,
                    "human_process_valid": item["ground_truth"]["process_valid"],
                    "first_error_step": item["ground_truth"]["first_error_step"],
                    "error_type": item["ground_truth"]["error_type"],
                    "is_evaluator_false_positive": (
                        evaluation["process_correct"] is False and item["ground_truth"]["process_valid"]
                        if evaluation["process_correct"] is not None else None
                    ),
                    "evidence": item["ground_truth"]["annotation_basis"],
                    "adjudicator": "",
                    "adjudication": "构造集金标；真实 Hy3 结果需独立人工复核",
                }
            )
        write_jsonl(output.with_name(output.stem + "_human_audit.jsonl"), audits)
    _print({key: value for key, value in result.items() if key != "records"} | {"output": str(output)})
    return 0 if result.get("failed_runs", 0) == 0 else 2


def command_audit_export(args: argparse.Namespace) -> int:
    benchmark = json.loads(args.benchmark.read_text(encoding="utf-8"))
    queue = build_audit_queue(benchmark)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite a file in which a person may already have started reviewing.
    with args.output.open("x", encoding="utf-8") as stream:
        for row in queue:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    _print({"pending_samples": len(queue), "output": str(args.output)})
    return 0


def command_audit_summary(args: argparse.Namespace) -> int:
    benchmark = json.loads(args.benchmark.read_text(encoding="utf-8"))
    annotations = [json.loads(line) for line in args.annotations.read_text(encoding="utf-8").splitlines() if line.strip()]
    result = summarize_audits(benchmark, annotations)
    if args.output:
        if args.output.resolve() in (args.benchmark.resolve(), args.annotations.resolve()):
            raise ValueError("汇总输出不得覆盖基准或人工标注文件")
        write_json(args.output, result)
    _print(result)
    return 0


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须为正整数")
    return number


def _nonnegative_seconds(value: str) -> float:
    import math
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 30:
        raise argparse.ArgumentTypeError("等待时间须为 0 到 30 秒的有限数值")
    return number


def command_serve(args: argparse.Namespace) -> int:
    from .web import serve

    serve(host=args.host, port=args.port)
    return 0


def command_database_upgrade(_: argparse.Namespace) -> int:
    from .api.database_admin import upgrade_database

    _print(upgrade_database())
    return 0


def command_database_status(_: argparse.Namespace) -> int:
    from .api.database_admin import database_status

    _print(database_status())
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tracejudge", description="Hy3 可验证算法过程评估")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="检查 Hy3 /v1/models 连接").set_defaults(func=command_doctor)
    subparsers.add_parser("list", help="列出内置题目").set_defaults(func=command_list)

    solve = subparsers.add_parser("solve", help="调用 Hy3 解题并完成过程评估")
    solve.add_argument("--problem", required=True)
    solve.add_argument("--hypothesis-examples", type=_positive_int, default=60)
    solve.add_argument("--no-hy3-review", action="store_true")
    solve.add_argument(
        "--review-mode",
        choices=("single", "supervisor", "swarm"),
        default="supervisor",
        help="过程审查拓扑；supervisor=5个并行专业Agent，swarm=冲突时再仲裁",
    )
    solve.add_argument("--output", type=Path)
    solve.set_defaults(func=command_solve)

    evaluate = subparsers.add_parser("evaluate", help="评估已有结构化答案")
    evaluate.add_argument("--problem", required=True)
    evaluate.add_argument("--answer", type=Path, required=True)
    evaluate.add_argument("--hypothesis-examples", type=_positive_int, default=60)
    evaluate.add_argument("--hy3-review", action="store_true")
    evaluate.add_argument(
        "--review-mode",
        choices=("single", "supervisor", "swarm"),
        default="supervisor",
    )
    evaluate.add_argument("--output", type=Path)
    evaluate.set_defaults(func=command_evaluate)

    benchmark = subparsers.add_parser("benchmark", help="运行构造集或真实 Hy3 基准")
    benchmark.add_argument("--source", choices=("fixtures", "hy3"), default="fixtures")
    benchmark.add_argument("--hypothesis-examples", type=_positive_int, default=20)
    selection = benchmark.add_mutually_exclusive_group()
    selection.add_argument("--limit", type=_positive_int)
    selection.add_argument("--per-difficulty", type=_positive_int, help="每个难度等量选题，按题库固定顺序")
    benchmark.add_argument("--hy3-review", action="store_true", help="为构造集启用真实 Hy3 语义复核（会调用模型）")
    benchmark.add_argument(
        "--fixture-profile",
        action="append",
        choices=("gold", "wrong", "unsupported_correct", "keyword_only", "negated_fault",
                 "correct_code_wrong_proof", "condition_overgeneralization"),
        help="仅运行指定构造类型；可重复传入，避免为无关样本调用模型",
    )
    benchmark.add_argument(
        "--fixture-sample", action="append",
        help="仅运行指定 sample_id；可重复传入，用于精确重试弃判样本",
    )
    benchmark.add_argument(
        "--fixture-delay", type=_nonnegative_seconds, default=0.0,
        help="构造样本语义评审之间的冷却秒数，TokenHub高负载时推荐30",
    )
    benchmark.add_argument(
        "--tier",
        choices=("seed", "external", "all"),
        default="all",
        help="hy3 基准的题集范围；seed=仅原创题，external=仅导入题，all=全部",
    )
    benchmark.add_argument(
        "--review-mode",
        choices=("single", "supervisor", "swarm"),
        default="supervisor",
    )
    benchmark.add_argument("--output", type=Path)
    benchmark.add_argument("--resume", action="store_true", help="读取相同输出文件，跳过已完成题并复用已生成答案")
    benchmark.add_argument("--retry-failed", action="store_true", help="续跑时重新尝试已失败题")
    benchmark.add_argument("--max-attempts", type=_positive_int, default=2, help="每道待执行题本次最多尝试次数，默认 2")
    benchmark.add_argument("--retry-delay", type=_nonnegative_seconds, default=1.0, help="指数退避初始秒数，单次最多 30 秒")
    benchmark.set_defaults(func=command_benchmark)

    audit_export = subparsers.add_parser("audit-export", help="导出不含预测结论的人工复核 JSONL")
    audit_export.add_argument("--benchmark", type=Path, required=True)
    audit_export.add_argument("--output", type=Path, required=True)
    audit_export.set_defaults(func=command_audit_export)
    audit_summary = subparsers.add_parser("audit-summary", help="校验人工记录并计算定位率、误报比例和覆盖率")
    audit_summary.add_argument("--benchmark", type=Path, required=True)
    audit_summary.add_argument("--annotations", type=Path, required=True)
    audit_summary.add_argument("--output", type=Path)
    audit_summary.set_defaults(func=command_audit_summary)

    serve_parser = subparsers.add_parser("serve", help="启动生产级 Web/API 服务")
    serve_parser.add_argument("--host", help="监听地址；默认读取 WEB_HOST")
    serve_parser.add_argument("--port", type=int, help="监听端口；默认读取 WEB_PORT")
    serve_parser.set_defaults(func=command_serve)

    database = subparsers.add_parser("database", help="管理 MySQL 任务数据库")
    database_commands = database.add_subparsers(dest="database_command", required=True)
    database_commands.add_parser("upgrade", help="创建数据库并升级到最新结构").set_defaults(
        func=command_database_upgrade
    )
    database_commands.add_parser("status", help="检查数据库连接和迁移版本").set_defaults(
        func=command_database_status
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("评测已中断；真实基准的已落盘答案与结果可通过相同参数加 --resume 继续。", file=sys.stderr)
        return 130
    except (Hy3APIError, KeyError, ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
