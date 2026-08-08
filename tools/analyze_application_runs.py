"""Compare repeated application dry runs and optional human ground truth."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any


def load_payload(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def run_records(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {record["value"]["job_id"]: record["value"]
            for record in payload["records"] if record.get("ok")}


def normalized_outcome(value: dict[str, Any]) -> str:
    if value.get("is_application"):
        return "form"
    state = str(value.get("workflow_terminal") or "")
    if state in {"account_wall", "bot_wall", "stable_failure"}:
        return state
    return "no_form"


def analyze(paths: list[Path], ground_truth: dict[str, str] | None = None) -> dict[str, Any]:
    payloads = [load_payload(path) for path in paths]
    runs = [run_records(payload) for payload in payloads]
    ids = sorted(set().union(*(run for run in runs)))
    jobs = []
    for job_id in ids:
        observed = [normalized_outcome(run[job_id]) if job_id in run else "missing"
                    for run in runs]
        expected = (ground_truth or {}).get(job_id)
        row = {"job_id": job_id, "observed": observed,
               "deterministic": len(set(observed)) == 1,
               "expected": expected}
        if expected is not None:
            row["matches_ground_truth"] = all(value == expected for value in observed)
        jobs.append(row)
    deterministic = sum(row["deterministic"] for row in jobs)
    labelled = [row for row in jobs if row["expected"] is not None]
    concurrency = []
    for payload in payloads:
        records = payload.get("records") or []
        values = [record.get("value") or {} for record in records if record.get("ok")]
        delays = [float((value.get("diagnostics") or {}).get("event_loop_delay_ms") or 0)
                  for value in values]
        wall_ms = float((payload.get("meta") or {}).get("wall_ms") or 0)
        concurrency.append({
            "workers": int((payload.get("meta") or {}).get("workers_effective") or 0),
            "wall_ms": wall_ms, "jobs_per_second": round(len(records) * 1000 / wall_ms, 3)
                if wall_ms else 0,
            "worker_failures": sum(not record.get("ok") for record in records),
            "workflow_failures": sum(value.get("status") in {
                "navigation_failed", "workflow_failed"} for value in values),
            "forms": sum(value.get("is_application") is True for value in values),
            "event_loop_p95_ms": sorted(delays)[min(len(delays) - 1, int(len(delays) * .95))]
                if delays else 0,
        })
    distinct_workers = {row["workers"] for row in concurrency}
    recommendation = None
    recommendation_reason = "run at least two worker counts; no guess was made"
    if len(distinct_workers) >= 2:
        minimum_failures = min(row["workflow_failures"] for row in concurrency)
        eligible = [row for row in concurrency
                    if row["workflow_failures"] <= minimum_failures + 1
                    and row["event_loop_p95_ms"] <= 250]
        if eligible:
            recommendation = max(eligible, key=lambda row: row["jobs_per_second"])["workers"]
            recommendation_reason = (
                "highest measured throughput within reliability and event-loop limits"
            )
        else:
            recommendation_reason = (
                "no measured worker count stayed within the 250ms event-loop limit; "
                "reduce browser pressure before raising concurrency"
            )
    return {
        "runs": len(runs), "jobs": len(jobs), "deterministic": deterministic,
        "transient": len(jobs) - deterministic,
        "outcomes": dict(Counter(value for row in jobs for value in row["observed"])),
        "ground_truth": {
            "labelled": len(labelled),
            "matching": sum(bool(row.get("matches_ground_truth")) for row in labelled),
        },
        "concurrency": {"runs": concurrency, "recommended_workers": recommendation,
                        "reason": recommendation_reason},
        "records": jobs,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", nargs="+", type=Path)
    parser.add_argument("--ground-truth", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    truth = None
    if args.ground_truth:
        truth = json.loads(args.ground_truth.read_text(encoding="utf-8"))["jobs"]
    result = analyze(args.results, truth)
    rendered = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
