"""Offline task-level metrics for recorded application dry runs.

This is deliberately a benchmark, not harness runtime code. It joins the application
attempt log, session journals, and an optional Codex rollout without reading prompts or
reasoning text.
"""
from __future__ import annotations

import argparse
import bisect
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


def _jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in {path}:{line_number}") from exc
    return rows


def _epoch(stamp: str) -> float:
    return datetime.fromisoformat(stamp).timestamp()


def _selected_ids(manifest: Path | None, latest: dict[str, dict[str, Any]]) -> list[str]:
    if manifest is None:
        return sorted(latest)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    requested = [row["attempt"] for row in data["results"]]
    missing = [attempt_id for attempt_id in requested if attempt_id not in latest]
    if missing:
        raise ValueError(f"manifest attempts missing from log: {missing}")
    return requested


def _required_split(row: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    fields = row.get("required_unfilled") or []
    human = [field for field in fields if field.get("status") in {"missing", "skipped"}]
    technical = [field for field in fields if field.get("status") == "failed"]
    return human, technical


def _technical_evidence(field: dict[str, Any]) -> dict[str, Any]:
    keys = ("label", "name", "kind", "status", "reason", "source")
    evidence = {key: field[key] for key in keys if field.get(key) is not None}
    if evidence.get("label"):
        evidence["label"] = " ".join(str(evidence["label"]).split())
    if field.get("candidates"):
        evidence["candidates"] = field["candidates"][:5]
    return evidence


def _harness_metrics(root: Path) -> dict[str, Any]:
    invokes: set[tuple[Any, ...]] = set()
    retryable_failures = 0
    for journal in (root / "recordings").glob("*/session.jsonl"):
        for row in _jsonl(journal):
            if row.get("kind") == "invoke":
                invokes.add((row.get("ts"), row.get("ms_total"), row.get("source_lines")))
            outcome = row.get("outcome") or {}
            if row.get("kind") == "call" and not outcome.get("ok", True):
                retryable_failures += int(bool(outcome.get("retryable")))
    return {
        "invocations": len(invokes),
        "retryable_helper_failures": retryable_failures,
        "note": "helper failures are diagnostic; only repeated attempt IDs count as retries",
    }


def _codex_metrics(path: Path, *, started: float, finished: float,
                   marker: str) -> dict[str, Any]:
    tools = []
    usages = []
    for row in _jsonl(path):
        payload = row.get("payload") or {}
        if row.get("type") == "response_item" and payload.get("type") in {
            "custom_tool_call", "function_call"
        }:
            tools.append((_epoch(row["timestamp"]), payload))
        if (row.get("type") == "event_msg" and payload.get("type") == "token_count"
                and (payload.get("info") or {}).get("last_token_usage")):
            usages.append((_epoch(row["timestamp"]), payload["info"]["last_token_usage"]))

    launches = [event for event in tools
                if started - 120 <= event[0] <= started
                and marker in str(event[1].get("input") or event[1].get("arguments") or "")]
    window_start = max((event[0] for event in launches), default=started)
    selected = [event for event in tools if window_start <= event[0] <= finished]
    usage_times = [event[0] for event in usages]
    paired: list[tuple[dict[str, Any], dict[str, Any]]] = []
    taken: set[int] = set()
    for timestamp, tool in selected:
        index = bisect.bisect_left(usage_times, timestamp)
        if index >= len(usages) or index in taken:
            continue
        taken.add(index)
        paired.append((tool, usages[index][1]))

    def total(key: str) -> int:
        return sum(int(usage.get(key) or 0) for _, usage in paired)

    polling_pairs = [
        (tool, usage) for tool, usage in paired
        if tool.get("name") == "wait"
        or "tools.write_stdin" in str(tool.get("input") or tool.get("arguments") or "")
    ]
    input_tokens = total("input_tokens")
    cached_tokens = total("cached_input_tokens")
    polling_input = sum(int(usage.get("input_tokens") or 0)
                        for _, usage in polling_pairs)
    polling_cached = sum(int(usage.get("cached_input_tokens") or 0)
                         for _, usage in polling_pairs)
    return {
        "invocations": len(paired),
        "polling_invocations": len(polling_pairs),
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "uncached_input_tokens": input_tokens - cached_tokens,
        "output_tokens": total("output_tokens"),
        "reasoning_output_tokens": total("reasoning_output_tokens"),
        "polling_tokens": {
            "input": polling_input,
            "cached_input": polling_cached,
            "uncached_input": polling_input - polling_cached,
            "output": sum(int(usage.get("output_tokens") or 0)
                          for _, usage in polling_pairs),
        },
        "usage_coverage": {"matched": len(paired), "tool_calls": len(selected)},
        "privacy": "reads tool names, timestamps, and usage only; ignores message content",
    }


def measure(attempts_path: Path, *, manifest: Path | None = None,
            codex_transcript: Path | None = None) -> dict[str, Any]:
    attempts = _jsonl(attempts_path)
    if not attempts:
        raise ValueError(f"no attempts found in {attempts_path}")
    latest = {row["attempt"]: row for row in attempts}
    counts = Counter(row["attempt"] for row in attempts)
    selected = [latest[attempt_id] for attempt_id in _selected_ids(manifest, latest)]

    applications = []
    for row in selected:
        human, technical = _required_split(row)
        applications.append({
            "attempt": row["attempt"],
            "dry_run_success": bool(row.get("filled_count"))
            and not row.get("errors") and not row.get("submitted"),
            "autonomous_ready": not human and not technical,
            "filled_fields": int(row.get("filled_count") or 0),
            "human_intervention_fields": len(human),
            "technical_blocker_fields": len(technical),
            "technical_blockers": [_technical_evidence(field) for field in technical],
            "retries": counts[row["attempt"]] - 1,
        })

    started = min(float(row["started"]) for row in attempts)
    finished = max(float(row["finished"]) for row in attempts)
    result = {
        "run": {
            "applications": len(latest),
            "attempt_rows": len(attempts),
            "retries": sum(count - 1 for count in counts.values()),
            "duration_s": round(finished - started, 3),
            "submitted": sum(bool(row.get("submitted")) for row in attempts),
        },
        "selected": {
            "applications": len(applications),
            "dry_run_successes": sum(row["dry_run_success"] for row in applications),
            "autonomous_ready": sum(row["autonomous_ready"] for row in applications),
            "needs_human_intervention": sum(
                row["human_intervention_fields"] > 0 for row in applications),
            "human_intervention_fields": sum(
                row["human_intervention_fields"] for row in applications),
            "has_technical_blockers": sum(
                row["technical_blocker_fields"] > 0 for row in applications),
            "technical_blocker_fields": sum(
                row["technical_blocker_fields"] for row in applications),
        },
        "harness": _harness_metrics(attempts_path.parent.parent),
        "model": None,
        "applications": applications,
    }
    if codex_transcript:
        result["model"] = _codex_metrics(
            codex_transcript, started=started, finished=finished,
            marker=attempts_path.parent.parent.name,
        )
        model = result["model"]
        model["amortized_per_application"] = {
            "invocations": round(model["invocations"] / len(latest), 3),
            "input_tokens": round(model["input_tokens"] / len(latest)),
            "uncached_input_tokens": round(model["uncached_input_tokens"] / len(latest)),
            "output_tokens": round(model["output_tokens"] / len(latest)),
        }
        model["attribution"] = (
            "shared across the batch; per-application values are amortized, not exact"
        )
    return result


def decision_pack(result: dict[str, Any]) -> dict[str, Any]:
    """Only what the model needs for its next decision; full logs remain on disk."""
    selected = result["selected"]
    if result["run"]["submitted"]:
        next_action = "stop"
    elif selected["technical_blocker_fields"]:
        next_action = "resolve_technical_blockers"
    elif selected["human_intervention_fields"]:
        next_action = "request_human_input"
    else:
        next_action = "await_explicit_submission_authorization"
    return {
        "next_action": next_action,
        "safety": {"submitted": result["run"]["submitted"]},
        "outcomes": selected,
        "retries": result["run"]["retries"],
        "technical_blockers": [
            {"attempt": row["attempt"], "evidence": row["technical_blockers"]}
            for row in result["applications"] if row["technical_blockers"]
        ],
    }


def render(result: dict[str, Any]) -> str:
    run, selected, harness, model = (
        result["run"], result["selected"], result["harness"], result["model"])
    lines = [
        "application decision baseline",
        (f"run       {run['applications']} applications · {run['attempt_rows']} attempts · "
         f"{run['retries']} retry · {run['submitted']} submitted"),
        (f"selected  {selected['dry_run_successes']}/{selected['applications']} "
         f"dry runs succeeded · {selected['autonomous_ready']} autonomous-ready"),
        (f"human     {selected['needs_human_intervention']} applications · "
         f"{selected['human_intervention_fields']} required fields"),
        (f"technical {selected['has_technical_blockers']} applications · "
         f"{selected['technical_blocker_fields']} fields"),
        (f"harness   {harness['invocations']} invocations · "
         f"{harness['retryable_helper_failures']} retryable helper failures"),
    ]
    if model:
        lines.extend([
            (f"model     {model['invocations']} invocations · "
             f"{model['polling_invocations']} polling "
             f"({model['polling_invocations'] / max(model['invocations'], 1):.1%})"),
            (f"tokens    {model['input_tokens']:,} input "
             f"({model['cached_input_tokens']:,} cached, "
             f"{model['uncached_input_tokens']:,} uncached) · "
             f"{model['output_tokens']:,} output"),
            (f"poll cost {model['polling_tokens']['input']:,} input · "
             f"{model['polling_tokens']['output']:,} output"),
            (f"per app   {model['amortized_per_application']['invocations']:.3f} "
             f"invocations · {model['amortized_per_application']['input_tokens']:,} input · "
             f"{model['amortized_per_application']['output_tokens']:,} output (amortized)"),
        ])
    lines.extend(["", "application                         ok  auto  human  tech  retry"])
    for row in result["applications"]:
        lines.append(
            f"{row['attempt'][:35]:<35} "
            f"{'yes' if row['dry_run_success'] else 'no ':>3}  "
            f"{'yes' if row['autonomous_ready'] else 'no ':>4}  "
            f"{row['human_intervention_fields']:>5}  "
            f"{row['technical_blocker_fields']:>4}  {row['retries']:>5}"
        )
    blockers = [(row["attempt"], blocker) for row in result["applications"]
                for blocker in row["technical_blockers"]]
    if blockers:
        lines.extend(["", "technical blocker evidence"])
        for attempt, blocker in blockers:
            label = " ".join(
                str(blocker.get("label") or blocker.get("name") or "unnamed").split()
            )[:100]
            detail = str(blocker.get("reason") or blocker.get("status") or "failed")
            source = f" · source={blocker['source']}" if blocker.get("source") else ""
            candidates = blocker.get("candidates") or []
            options = f" · candidates={candidates}" if candidates else ""
            lines.append(f"{attempt}: {label} — {detail}{source}{options}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("attempts", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--codex-transcript", type=Path)
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true")
    output.add_argument("--pack", action="store_true")
    args = parser.parse_args()
    result = measure(args.attempts, manifest=args.manifest,
                     codex_transcript=args.codex_transcript)
    if args.pack:
        print(json.dumps(decision_pack(result), separators=(",", ":"), sort_keys=True))
    else:
        print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
