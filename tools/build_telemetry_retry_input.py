"""Build a retry corpus from failed records in a job-form telemetry run."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--statuses", nargs="+", default=["workflow_failed", "navigation_failed"])
    args = parser.parse_args()

    payload = json.loads(args.results.read_text(encoding="utf-8"))
    statuses = set(args.statuses)
    jobs = [
        record["item"]
        for record in payload.get("records", [])
        if (record.get("value") or {}).get("status") in statuses
    ]
    output = {
        "generated_from": str(args.results.resolve()),
        "retry_statuses": sorted(statuses),
        "summary": {"jobs": len(jobs)},
        "jobs": jobs,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output.resolve()), "jobs": len(jobs)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
