"""Compare 100-job telemetry runs with application skills disabled and enabled."""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def metrics(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("records") or []
    values = [record.get("value") or {} for record in records]
    durations = [float(value.get("wall_ms") or 0) for value in values]
    statuses = Counter(str(value.get("status") or record.get("class") or "unknown")
                       for record, value in zip(records, values, strict=True))
    forms = sum(bool(value.get("is_application")) for value in values)
    fully_filled = sum(
        bool((value.get("fill") or {}).get("ok"))
        and not any(item.get("required") and item.get("status") != "planned"
                    for item in value.get("field_audit") or [])
        for value in values
    )
    matches = [match["id"] for value in values
               for match in (value.get("skills") or {}).get("matches") or []]
    return {
        "path": str(path),
        "records": len(records),
        "batch_wall_ms": float((payload.get("meta") or {}).get("wall_ms") or 0),
        "record_wall_ms": {
            "median": round(statistics.median(durations), 1) if durations else 0,
            "p90": round(percentile(durations, 0.90), 1),
            "max": round(max(durations), 1) if durations else 0,
        },
        "statuses": dict(sorted(statuses.items())),
        "forms_reached": forms,
        "fully_fillable": fully_filled,
        "skill_matches": len(matches),
        "skill_ids": dict(sorted(Counter(matches).items())),
        "skill_context_bytes": sum(int((value.get("skills") or {}).get("bytes") or 0)
                                   for value in values),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("control", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    control = metrics(args.control)
    candidate = metrics(args.candidate)
    comparison = {
        "control": control,
        "candidate": candidate,
        "delta": {
            "batch_wall_ms": round(candidate["batch_wall_ms"] - control["batch_wall_ms"], 1),
            "batch_percent": round(
                (candidate["batch_wall_ms"] / control["batch_wall_ms"] - 1) * 100, 2
            ) if control["batch_wall_ms"] else None,
            "forms_reached": candidate["forms_reached"] - control["forms_reached"],
            "fully_fillable": candidate["fully_fillable"] - control["fully_fillable"],
        },
    }
    text = json.dumps(comparison, indent=2, ensure_ascii=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
