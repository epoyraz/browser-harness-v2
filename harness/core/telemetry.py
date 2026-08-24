"""Local telemetry: what is actually being used, and what is actually failing.

v1 has 308 lines of this and v2 had none, which meant every priority call in the build was
a guess. The point is not analytics — it is answering "what should I fix next" from data
instead of memory.

**Aggregation, not a second capture path.** Every number here is derived from journals that
already exist (D11b). Recording added `frame` to the same entries; telemetry sums them.
There is no telemetry hook in the hot path, so the cost of having it is zero until you run
`bh stats`.

**Local by default, and privacy is structural rather than promised.** The rollup keeps
exactly four things per call: the helper name, the outcome class, the duration, and the CDP
round-trip count. URLs, selectors, typed values and JS source are never read out of the
journal — not redacted afterwards, but never selected in the first place. Nothing is sent
anywhere; `bh stats` prints, and `--json` writes a file you choose.

The signal that makes this worth having is the outcome-class histogram. v1 could not
produce it: every failure was a `str`, so "which failure mode dominates" was unanswerable.
With a closed enum it is a `Counter`.
"""
from __future__ import annotations

import os
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from harness.core import jsonl

#: Fields copied out of a journal entry. Everything else — args, urls, expressions,
#: values — is deliberately not read. Widening this list is a privacy decision.
KEEP = ("fn", "ms", "cdp")


def journals_root() -> Path:
    """Where `bh` writes session journals when `BH_JOURNAL` is not set explicitly."""
    if raw := os.environ.get("BH_JOURNAL_DIR"):
        return Path(raw).expanduser()
    return Path.home() / ".browser-harness" / "journals"


def find_journals(paths: Iterable[str | Path] | None = None) -> list[Path]:
    """Journal files, newest first. A directory contributes its `*.jsonl`."""
    out: list[Path] = []
    for raw in (paths if paths is not None else [journals_root()]):
        p = Path(raw).expanduser()
        if p.is_dir():
            out.extend(p.rglob("*.jsonl"))
        elif p.is_file():
            out.append(p)
    return sorted(out, key=lambda f: f.stat().st_mtime, reverse=True)


def _calls(files: Iterable[Path]) -> Iterator[dict[str, Any]]:
    """Every `call` entry across the journals, reduced to KEEP + its outcome class."""
    for file in files:
        for e in jsonl.read(file):
            if e.get("kind") != "call" or e.get("observability") == "recording":
                continue
            outcome = e.get("outcome") or {}
            row = {k: e.get(k) for k in KEEP}
            row["ok"] = bool(outcome.get("ok"))
            row["class"] = None if row["ok"] else str(outcome.get("class") or "unknown")
            yield row


def _protocol(files: Iterable[Path]) -> Iterator[dict[str, Any]]:
    """Sanitized protocol outcome only; request and response values are never selected."""
    for file in files:
        for entry in jsonl.read(file):
            if (entry.get("kind") == "cdp"
                    and entry.get("observability") != "recording"):
                yield {"ok": bool(entry.get("ok")),
                       "class": entry.get("error_class"),
                       "code": entry.get("error_code")}


def _recording(files: Iterable[Path]) -> Iterator[dict[str, Any]]:
    """Privacy-safe per-action recorder cost and suppression records.

    These fields are generated mechanically by the recorder.  URL, title, focused-element
    context, helper arguments, and form values are deliberately never selected.
    """
    for file in files:
        for entry in jsonl.read(file):
            if entry.get("kind") != "call":
                continue
            if entry.get("frame"):
                yield {
                    "retained": True,
                    "profile": str(entry.get("recording_profile") or "unknown"),
                    "screenshot_ms": float(entry.get("frame_screenshot_ms") or 0),
                    "recording_ms": float(entry.get("frame_recording_ms") or 0),
                    "cdp": int(entry.get("frame_cdp") or 0),
                    "bytes": int(entry.get("frame_bytes") or 0),
                }
            elif reason := entry.get("frame_suppressed"):
                yield {
                    "retained": False,
                    "profile": str(entry.get("recording_profile") or "unknown"),
                    "reason": str(reason),
                }


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def rollup(paths: Iterable[str | Path] | None = None) -> dict[str, Any]:
    """Aggregate journals into the four questions worth asking of a harness."""
    files = find_journals(paths)
    per: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "failed": 0, "ms": [], "cdp": 0})
    classes: Counter[str] = Counter()
    for row in _calls(files):
        fn = row["fn"] or "?"
        s = per[fn]
        s["calls"] += 1
        if isinstance(row["ms"], (int, float)):
            s["ms"].append(float(row["ms"]))
        if isinstance(row["cdp"], int):
            s["cdp"] += row["cdp"]
        if not row["ok"]:
            s["failed"] += 1
            classes[row["class"] or "unknown"] += 1

    helpers = []
    for fn, s in per.items():
        calls = s["calls"]
        helpers.append({
            "fn": fn, "calls": calls, "failed": s["failed"],
            "fail_rate": round(s["failed"] / calls, 3) if calls else 0.0,
            "p50_ms": round(_pct(s["ms"], 0.50), 1),
            "p95_ms": round(_pct(s["ms"], 0.95), 1),
            "cdp_total": s["cdp"],
            # The waste signal: round trips per call. v1's fill_input sat at 61.
            "cdp_per_call": round(s["cdp"] / calls, 2) if calls else 0.0,
        })
    helpers.sort(key=lambda h: -h["calls"])
    total = sum(h["calls"] for h in helpers)
    protocol_rows = list(_protocol(files))
    protocol_failed = [row for row in protocol_rows if not row["ok"]]
    recording_rows = list(_recording(files))
    retained = [row for row in recording_rows if row["retained"]]
    suppressed = [row for row in recording_rows if not row["retained"]]
    return {
        "journals": len(files),
        "calls": total,
        "failed": sum(h["failed"] for h in helpers),
        "helpers": helpers,
        "failure_classes": classes.most_common(),
        "protocol": {
            "calls": len(protocol_rows), "failed": len(protocol_failed),
            "failure_classes": Counter(str(row.get("class") or "unknown")
                                       for row in protocol_failed).most_common(),
            "failure_codes": Counter(str(row.get("code")) for row in protocol_failed
                                     if row.get("code") is not None).most_common(),
        },
        "observability": {
            "frames": len(retained),
            "screenshot_ms": round(sum(row["screenshot_ms"] for row in retained), 1),
            "wall_ms": round(sum(row["recording_ms"] for row in retained), 1),
            "cdp": sum(row["cdp"] for row in retained),
            "bytes": sum(row["bytes"] for row in retained),
            "profiles": Counter(row["profile"] for row in recording_rows).most_common(),
            "suppressed": len(suppressed),
            "suppressed_by_reason": Counter(
                row["reason"] for row in suppressed).most_common(),
        },
    }


def render(r: dict[str, Any], *, top: int = 12) -> list[str]:
    """Human lines. Leads with failures, because that is the actionable half."""
    if not r["calls"]:
        return [f"no calls recorded ({r['journals']} journal(s) found)",
                "set BH_JOURNAL=<file> when running `bh` to start recording one"]
    rate = r["failed"] / r["calls"]
    out = [(f"{r['calls']:,} calls across {r['journals']} session(s) · "
            f"{r['failed']:,} failed ({rate:.1%})"), ""]
    protocol = r.get("protocol") or {}
    if protocol.get("calls"):
        out.insert(1, (f"{protocol['calls']:,} sanitized CDP round trips · "
                       f"{protocol['failed']:,} failed or recovered"))
    recording = r.get("observability") or {}
    if recording.get("frames") or recording.get("suppressed"):
        out.insert(1, (
            f"recording observability · {recording.get('frames', 0):,} frame(s) · "
            f"{recording.get('cdp', 0):,} CDP · {recording.get('wall_ms', 0):,.1f} ms · "
            f"{recording.get('bytes', 0):,} bytes · "
            f"{recording.get('suppressed', 0):,} suppressed"))
    if r["failure_classes"]:
        out.append("failure classes — what to fix, in order:")
        width = max(len(c) for c, _ in r["failure_classes"])
        for cls, n in r["failure_classes"][:top]:
            out.append(f"  {n:>6}  {cls:<{width}}  {n / r['failed']:.0%} of failures")
        out.append("")
    out.append(f"{'helper':<20}{'calls':>8}{'fail':>7}{'p50':>9}{'p95':>9}{'cdp/call':>10}")
    for h in r["helpers"][:top]:
        fail = f"{h['fail_rate']:.0%}" if h["failed"] else "-"
        out.append(f"{h['fn'][:20]:<20}{h['calls']:>8,}{fail:>7}"
                   f"{h['p50_ms']:>8.0f}m{h['p95_ms']:>8.0f}m{h['cdp_per_call']:>10.2f}")
    return out
