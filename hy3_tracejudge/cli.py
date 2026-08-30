from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .catalog import ROOT, get_problem, load_problems
from .evaluator import evaluate_answer, summarize_results, validate_evaluator
from .fixtures import build_labeled_samples
from .hy3_client import Hy3APIError, Hy3Client
from .reporting import write_json, write_jsonl, write_results_csv


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
    records = []
    for sample in samples:
        evaluation = evaluate_answer(
            get_problem(sample["problem_id"]),
            sample["answer"],
            hypothesis_examples=args.hypothesis_examples,
        )
        records.append({**sample, "evaluation": evaluation})
    evaluations = [item["evaluation"] for item in records]
    return {
        "run_type": "evaluator_validation_fixtures",
        "disclaimer": "人工构造单点错误，仅验证评估器；不是 Hy3 模型能力结果。",
        "summary": summarize_results(evaluations),
        "validity": validate_evaluator(records),
        "records": records,
    }


def _filter_tier(problems: list[dict[str, Any]], tier: str) -> list[dict[str, Any]]:
    if tier == "all":
        return problems
    return [problem for problem in problems if problem.get("tier", "seed") == tier]


def _hy3_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    client = Hy3Client()
    selected = _filter_tier(load_problems(), args.tier)
    if args.limit:
        selected = selected[: args.limit]
    records = []
    for problem in selected:
        try:
            answer, generation = client.solve(problem)
            evaluation = evaluate_answer(
                problem,
                answer,
                hypothesis_examples=args.hypothesis_examples,
                hy3_client=client,
                review_mode=args.review_mode,
            )
            records.append(
                {
                    "sample_id": f"hy3-{problem['id']}",
                    "problem_id": problem["id"],
                    "difficulty": problem["difficulty"],
                    "answer": answer,
                    "generation_metadata": generation,
                    "evaluation": evaluation,
                }
            )
        except Hy3APIError as exc:
            records.append(
                {
                    "sample_id": f"hy3-{problem['id']}",
                    "problem_id": problem["id"],
                    "difficulty": problem["difficulty"],
                    "run_error": str(exc),
                }
            )
    completed = [item["evaluation"] for item in records if "evaluation" in item]
    return {
        "run_type": "hy3_model_benchmark",
        "model": client.config.model,
        "endpoint": client.config.base_url,
        "review_mode": args.review_mode,
        "tier": args.tier,
        "summary": summarize_results(completed),
        "completed": len(completed),
        "failed_runs": len(records) - len(completed),
        "records": records,
    }


def command_benchmark(args: argparse.Namespace) -> int:
    result = _fixture_benchmark(args) if args.source == "fixtures" else _hy3_benchmark(args)
    output = args.output or ROOT / "reports" / f"{args.source}_benchmark.json"
    write_json(output, result)
    records = result["records"]
    write_jsonl(output.with_suffix(".jsonl"), records)
    write_results_csv(output.with_suffix(".csv"), records)
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
                    "process_issue_flagged": not evaluation["process_correct"],
                    "human_process_valid": item["ground_truth"]["process_valid"],
                    "first_error_step": item["ground_truth"]["first_error_step"],
                    "error_type": item["ground_truth"]["error_type"],
                    "is_evaluator_false_positive": (
                        not evaluation["process_correct"] and item["ground_truth"]["process_valid"]
                    ),
                    "evidence": item["ground_truth"]["annotation_basis"],
                    "adjudicator": "",
                    "adjudication": "构造集金标；真实 Hy3 结果需独立人工复核",
                }
            )
        write_jsonl(output.with_name(output.stem + "_human_audit.jsonl"), audits)
    _print({key: value for key, value in result.items() if key != "records"} | {"output": str(output)})
    return 0 if result.get("failed_runs", 0) == 0 else 2


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
    solve.add_argument("--hypothesis-examples", type=int, default=60)
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
    evaluate.add_argument("--hypothesis-examples", type=int, default=60)
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
    benchmark.add_argument("--hypothesis-examples", type=int, default=20)
    benchmark.add_argument("--limit", type=int)
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
    benchmark.set_defaults(func=command_benchmark)

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
    except (Hy3APIError, KeyError, ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
