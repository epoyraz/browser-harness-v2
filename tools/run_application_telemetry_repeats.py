"""Run the non-submitting 100-job collector repeatedly into isolated artifacts."""
from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--workers", default="6,8,10",
                        help="one count or comma-separated pilot counts")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "outputs" / "job-form-telemetry-repeats")
    parser.add_argument("--ground-truth", type=Path)
    args = parser.parse_args()
    if not 1 <= args.runs <= 10:
        raise SystemExit("--runs must be between 1 and 10")
    worker_plan = [int(value) for value in args.workers.split(",")]
    if not worker_plan or any(not 1 <= value <= 10 for value in worker_plan):
        raise SystemExit("--workers values must be between 1 and 10")
    source = (ROOT / "tools" / "collect_job_form_telemetry.py").read_text(encoding="utf-8")
    results = []
    for number in range(1, args.runs + 1):
        workers = worker_plan[(number - 1) % len(worker_plan)]
        run = args.output / f"run-{number}"
        run.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "BH_APPLICATION_TELEMETRY_OUT": str(run),
               "BH_APPLICATION_WORKERS": str(workers), "BH_CDP_TRACE": "1",
               "BH_JOURNAL": str(run / "journal.jsonl")}
        subprocess.run(["uv", "run", "bh"], cwd=ROOT, env=env, input=source,
                       text=True, check=True)
        results.append(run / "results.json")
    command = ["uv", "run", "python", "tools/analyze_application_runs.py",
               *(str(path) for path in results), "--output", str(args.output / "analysis.json")]
    if args.ground_truth:
        command.extend(["--ground-truth", str(args.ground_truth)])
    subprocess.run(command, cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
