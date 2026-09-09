from __future__ import annotations

import argparse
import json
from pathlib import Path

from hy3_tracejudge.merging import merge_fixture_reports
from hy3_tracejudge.reporting import write_json, write_jsonl, write_results_csv


def main() -> None:
    parser = argparse.ArgumentParser(description="去重合并构造集评测及定向重试")
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    reports = [(str(path), json.loads(path.read_text(encoding="utf-8"))) for path in args.inputs]
    merged = merge_fixture_reports(reports)
    write_json(args.output, merged)
    write_jsonl(args.output.with_suffix(".jsonl"), merged["records"])
    write_results_csv(args.output.with_suffix(".csv"), merged["records"])
    print(json.dumps({"samples": len(merged["records"]),
                      "process_detection": merged["process_detection"],
                      "output": str(args.output)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
