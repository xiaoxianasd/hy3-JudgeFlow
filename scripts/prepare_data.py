from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent


def write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for value in values:
            stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def fingerprint(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def prepare_seed(output: Path) -> None:
    problems = json.loads((ROOT / "data" / "problems.json").read_text(encoding="utf-8"))
    normalized = []
    for problem in problems:
        normalized.append(
            {
                "id": problem["id"],
                "source": "tracejudge_seed_v1",
                "source_record_id": problem["id"],
                "title": problem["title"],
                "statement": problem["statement"],
                "difficulty": problem["difficulty"],
                "difficulty_rank": problem["difficulty_rank"],
                "function_name": problem["function_name"],
                "tests": problem["public_tests"] + problem["hidden_tests"],
                "reference_solution": problem["reference_solution"],
                "gold_steps": problem["gold_steps"],
                "rubric": problem["rubric"],
                "content_sha256": fingerprint(problem),
            }
        )
    write_jsonl(output / "problems.jsonl", normalized)
    write_jsonl(output / "dev.jsonl", normalized[::2])
    write_jsonl(output / "test.jsonl", normalized[1::2])
    split_manifest = {
        "version": "tracejudge_seed_v1",
        "construction": "原创参数化题；按固定奇偶序号划分 dev/test，仅供工程验收",
        "difficulty_basis": "状态/算法复杂度、证明负担、边界与反例强度",
        "counts": {"all": len(normalized), "dev": len(normalized[::2]), "test": len(normalized[1::2])},
        "ids": {"dev": [x["id"] for x in normalized[::2]], "test": [x["id"] for x in normalized[1::2]]},
    }
    (output / "split_manifest.json").write_text(
        json.dumps(split_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare the reproducible seed data stack")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "processed")
    args = parser.parse_args()
    prepare_seed(args.output)
    print(f"prepared: {args.output}")


if __name__ == "__main__":
    main()
