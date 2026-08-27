"""Trace rendering (DESIGN.md D11b, TODO 26).

Reads the journal and renders the span tree with **CDP round-trip counts** — the
actionable number. v1's `fill_input` spent 61 round trips on a 20-character field and
nothing surfaced it until someone ran a benchmark; here `cdp=61` sits on the line.

Success is silent by design: the journal is always written, this renderer only speaks
when asked (`bh trace <file>`) or when a run dies (`tail=` gives the last N top-level
spans for that). Counts land on the *innermost* span, so waste shows up exactly where it
was spent — a `fill_form` parent with `cdp=0` over a child with `cdp=61` names the layer
to fix.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any


def _seq(entry: dict[str, Any]) -> int:
    """Order spans by allocation, not file position — entries close inner-first."""
    try:
        return int(str(entry.get("id", "")).rsplit(".", 1)[-1])
    except ValueError:
        return 0


def _fmt(entry: dict[str, Any], depth: int) -> str:
    args = " ".join(f"{k}={str(v)[:48]}" for k, v in (entry.get("args") or {}).items())
    outcome = entry.get("outcome") or {}
    if outcome.get("ok"):
        status = "ok"
    else:
        status = f"FAIL {outcome.get('class', '?')}"
        if outcome.get("detail"):
            status += f" — {str(outcome['detail'])[:80]}"
    head = f"{'  ' * depth}{entry.get('fn', '?')}" + (f" {args}" if args else "")
    return f"{head:<64} {entry.get('ms', 0):>8}ms  cdp={entry.get('cdp', 0):<3} {status}"


def render(entries: Iterable[dict[str, Any]], *, tail: int | None = None) -> list[str]:
    """Span tree as lines. `tail=N` keeps only the last N top-level spans — the
    dump-on-error view: what was the run doing just before it died."""
    calls = [e for e in entries if e.get("kind") == "call"]
    children: dict[str, list[dict[str, Any]]] = defaultdict(list)
    roots: list[dict[str, Any]] = []
    for e in calls:
        (children[e["parent"]] if e.get("parent") else roots).append(e)
    roots.sort(key=_seq)
    if tail is not None:
        roots = roots[-tail:]
    out: list[str] = []

    def emit(entry: dict[str, Any], depth: int) -> None:
        out.append(_fmt(entry, depth))
        for child in sorted(children.get(entry["id"], []), key=_seq):
            emit(child, depth + 1)

    for root in roots:
        emit(root, 0)
    return out
