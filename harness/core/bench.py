"""Step and time accounting: `bh bench` (D0 made measurable).

The design doc's founding measurement is that ~90% of an agent task's wall clock is model
thinking and ~0.03% is harness primitives. If that is true, "make the harness fast" is
mostly the wrong optimisation — the lever is **making fewer decisions**, and the unit of a
decision is one `bh` invocation. So the headline number here is `steps`, not milliseconds.

Four buckets, and the reason each is separate:

  think    between invocations — the model deciding what to run next. The harness never
           sees it, so it is inferred from the gap between one invoke ending and the next
           beginning. Almost always the largest bucket, and the only one step-collapsing
           actually reduces.
  connect  process start → session ready. Paid ONCE PER STEP, so it multiplies with step
           count: it is the hidden tax that makes an 11-step task worse than 11× a 1-step
           task's work.
  harness  inside helper spans — our own CDP round trips and in-page JS. The bucket
           everyone tries to optimise; usually the smallest.
  wait     inside the script but outside any span — page loads, network, sleeps. Invisible
           before this module, and often larger than `harness`.

`collapsible()` is the actionable half: consecutive steps that only *read* are exploration
that a single richer call could have answered.
"""
from __future__ import annotations

import itertools
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

#: Helpers that only observe. A run of these across steps is exploration — the model
#: looking around because one call did not tell it enough — and exploration is the
#: cheapest thing to collapse, because merging reads can never change page state.
READ_ONLY = frozenset({
    "snapshot", "see", "page_text", "form_schema", "js", "screenshot",
    "capture_screenshot", "frames", "wait_for", "wait_lifecycle",
})


def _entries(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in paths:
        p = Path(raw).expanduser()
        files = sorted(p.rglob("*.jsonl")) if p.is_dir() else [p]
        for f in files:
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue      # a run killed mid-write loses its last line, not the file
    return sorted(out, key=lambda e: e.get("ts", 0))


def steps(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    """One record per `bh` invocation, with its calls attributed to it.

    Calls are assigned to the invoke that *follows* them, because the invoke entry is
    written last — it can only be written once the run it describes has finished.
    """
    entries = _entries(paths)
    out: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for e in entries:
        kind = e.get("kind")
        if kind == "call":
            pending.append(e)
        elif kind == "invoke":
            top = [c for c in pending if not c.get("parent")]
            harness_ms = sum(float(c.get("ms") or 0) for c in top)
            total = float(e.get("ms_total") or 0)
            connect = float(e.get("ms_connect") or 0)
            out.append({
                "ts": e.get("ts", 0), "ok": bool(e.get("ok", True)),
                "ms_total": total, "ms_connect": connect,
                "ms_harness": round(harness_ms, 1),
                # Inside the script but inside no span: page loads, network, sleeps.
                "ms_wait": round(max(0.0, total - connect - harness_ms), 1),
                "cdp": sum(int(c.get("cdp") or 0) for c in pending),
                "calls": len(pending),
                "fns": [c.get("fn") for c in top],
                "source_lines": e.get("source_lines"),
                "outcome": e.get("outcome") or {"ok": True},
            })
            pending = []
    return out


def collapsible(step_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Runs of consecutive read-only steps — exploration one richer call could replace.

    Reads are safe to merge by construction: nothing observed can change what a later
    observation would have seen, so combining them cannot alter behaviour. A run of length
    N could have been 1 step, saving N-1 think-times plus N-1 connects.
    """
    runs: list[dict[str, Any]] = []
    cur: list[int] = []
    for i, s in enumerate(step_list):
        if s["fns"] and all(f in READ_ONLY for f in s["fns"]):
            cur.append(i)
        else:
            if len(cur) > 1:
                runs.append({"from": cur[0] + 1, "to": cur[-1] + 1, "steps": len(cur)})
            cur = []
    if len(cur) > 1:
        runs.append({"from": cur[0] + 1, "to": cur[-1] + 1, "steps": len(cur)})
    return runs


def rollup(paths: Iterable[str | Path], *, think_ms: float | None = None) -> dict[str, Any]:
    """Aggregate. `think_ms` overrides the inferred per-step thinking time — useful when
    the journals come from a scripted benchmark, where the gaps are a harness driving
    itself rather than a model deciding anything."""
    st = steps(paths)
    empty = {"steps": 0, "failed": 0, "cdp": 0, "calls": 0,
             "buckets": {"think": 0.0, "connect": 0.0, "harness": 0.0, "wait": 0.0},
             "total_ms": 0.0, "think_per_step_ms": think_ms or 0.0,
             "think_inferred": think_ms is None, "collapsible": [], "step_list": []}
    if not st:
        return empty          # full shape even when nothing ran, so callers cannot KeyError
    gaps: list[float] = []
    for a, b in itertools.pairwise(st):
        gap = (b["ts"] - a["ts"]) * 1000 - b["ms_total"]
        if 0 < gap < 600_000:
            gaps.append(gap)
    inferred = round(sum(gaps) / len(gaps), 1) if gaps else 0.0
    per_think = inferred if think_ms is None else think_ms
    think_total = per_think * max(0, len(st) - 1)
    buckets = {
        "think": round(think_total, 1),
        "connect": round(sum(s["ms_connect"] for s in st), 1),
        "harness": round(sum(s["ms_harness"] for s in st), 1),
        "wait": round(sum(s["ms_wait"] for s in st), 1),
    }
    return {
        "steps": len(st), "failed": sum(1 for s in st if not s["ok"]),
        "cdp": sum(s["cdp"] for s in st), "calls": sum(s["calls"] for s in st),
        "buckets": buckets, "total_ms": round(sum(buckets.values()), 1),
        "think_per_step_ms": per_think, "think_inferred": think_ms is None,
        "collapsible": collapsible(st), "step_list": st,
    }


def render(r: dict[str, Any], *, verbose: bool = False) -> list[str]:
    if not r.get("steps"):
        return ["no invocations recorded",
                "run with BH_JOURNAL=<file> so each `bh` run records a step"]
    b, total = r["buckets"], r["total_ms"] or 1.0
    out = [f"{r['steps']} steps · {r['calls']} helper calls · {r['cdp']} CDP round trips"
           + (f" · {r['failed']} step(s) failed" if r["failed"] else ""), ""]
    out.append(f"{'where the wall clock went':<26}{'ms':>10}{'share':>8}")
    for name in ("think", "connect", "harness", "wait"):
        bar = "#" * round(28 * b[name] / total)
        out.append(f"  {name:<24}{b[name]:>10,.0f}{b[name] / total:>7.0%}  {bar}")
    out.append(f"  {'TOTAL':<24}{total:>10,.0f}")
    src = "inferred from gaps between steps" if r["think_inferred"] else "supplied"
    out.append(f"\nthink is {r['think_per_step_ms']:,.0f} ms/step ({src}); "
               f"connect is paid once per step, so both scale with STEP COUNT.")

    if r["collapsible"]:
        saved = sum(c["steps"] - 1 for c in r["collapsible"])
        cost = saved * (r["think_per_step_ms"] + b["connect"] / r["steps"])
        out.append(f"\ncollapsible: {len(r['collapsible'])} run(s) of read-only steps — "
                   f"{saved} step(s) removable, ~{cost / 1000:,.1f}s")
        for c in r["collapsible"]:
            out.append(f"  steps {c['from']}–{c['to']} are all reads "
                       f"({c['steps']} → 1)")
    if verbose:
        out.append(f"\n{'#':>3}  {'total':>8}{'conn':>7}{'harn':>7}{'wait':>8}{'cdp':>5}  calls")
        for i, s in enumerate(r["step_list"], 1):
            flag = "" if s["ok"] else "  FAILED"
            out.append(f"{i:>3}  {s['ms_total']:>8.0f}{s['ms_connect']:>7.0f}"
                       f"{s['ms_harness']:>7.0f}{s['ms_wait']:>8.0f}{s['cdp']:>5}  "
                       f"{','.join(s['fns'][:5]) or '-'}{flag}")
    return out
