"""Real think time, recovered from the agent's own transcript (`bh bench --from-transcript`).

`bench.py` calls `think` the dominant bucket but cannot actually see it. The harness is a
subprocess the model *spawns*: the interval between one `bh` exiting and the next starting
happens entirely outside our process, so the journal has no record of it. Until now that
left two bad options — infer it from journal timestamps, which in a scripted benchmark
measures a shell loop rather than a model (~0 ms), or pass a flat `--think`, which is a
guess. Measured against a real run, the guess was off by 2x.

Claude Code writes one JSONL transcript per session under
`~/.claude/projects/<slug>/<session-id>.jsonl`, and every `tool_use` / `tool_result`
carries a wall-clock timestamp. That gives the number directly:

    think for step N  ==  (tool_use N issued) - (tool_result N-1 arrived)

which is precisely "the model read the last result and wrote the next script".

**The reasoning text is not in the file** and this module does not want it: thinking blocks
persist as `{"type": "thinking", "thinking": "", "signature": "..."}` — empty text, opaque
signature. We need the clock, not the content.

Three corrections that separate a real measurement from a plausible one:

  prompts     a gap that spans a human typing is not the model thinking. The first tool
              call after a user message is excluded, which is what otherwise produced a
              17-hour "think time" across an overnight break.
  idle        gaps beyond `IDLE_S` are dropped as interruptions — but counted and
              reported, never silently.
  sidechains  subagent turns (`isSidechain`) run in parallel with the main loop; counting
              them would double-bill wall clock that overlapped.
"""
from __future__ import annotations

import bisect
import json
import re
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

#: Where Claude Code keeps transcripts, one directory per project.
PROJECTS = Path.home() / ".claude" / "projects"

#: A tool call that plausibly *is* a harness step. Used only for the flat fallback — when
#: journal steps align to the transcript by timestamp, the match is exact and this is moot.
HARNESS_RE = re.compile(r"browser-harness|harness[./]cli|(?<![\w./-])bh(?![\w-])")

#: Beyond this a "gap" is a human interruption, not a decision. Deliberately generous: a
#: hard turn on a large page dump can legitimately run into the minutes.
IDLE_S = 300.0

#: How far a journal `invoke` may sit from its transcript `tool_use` and still be the same
#: event. Covers process spawn plus clock skew; far below the smallest real think gap.
TOLERANCE_S = 45.0


def _epoch(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return None


def _entries(path: str | Path) -> Iterator[dict[str, Any]]:
    with Path(path).expanduser().open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue      # a session killed mid-write loses its last line, not the file


def _is_prompt(entry: dict[str, Any]) -> bool:
    """A genuine human turn, as opposed to a tool result wearing the `user` role."""
    if entry.get("type") != "user":
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result"
                       for b in content)
    return False


def _command(name: str | None, tool_input: dict[str, Any]) -> str:
    for key in ("command", "file_path", "pattern", "url", "prompt"):
        if (val := tool_input.get(key)):
            return f"{name}: {val}"[:200] if name else str(val)[:200]
    return (name or "?")


def gaps(path: str | Path, *, include_sidechain: bool = False) -> list[dict[str, Any]]:
    """Every tool call in the session, with the think time that preceded it.

    Ordered by wall clock rather than by file order: parallel tool calls share an assistant
    timestamp and their results return interleaved, so file order is not time order.
    """
    timeline: list[tuple[float, int, dict[str, Any]]] = []
    for entry in _entries(path):
        if entry.get("isSidechain") and not include_sidechain:
            continue
        when = _epoch(entry.get("timestamp"))
        if when is None:
            continue
        if _is_prompt(entry):
            timeline.append((when, 0, {"kind": "prompt"}))
            continue
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        usage = (entry.get("message") or {}).get("usage") or {}
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                timeline.append((when, 1, {
                    "kind": "use", "tool": block.get("name"),
                    "command": _command(block.get("name"), block.get("input") or {}),
                    "output_tokens": usage.get("output_tokens"),
                    "model": (entry.get("message") or {}).get("model"),
                    "effort": entry.get("effort"),
                }))
            elif block.get("type") == "tool_result":
                timeline.append((when, 2, {"kind": "result"}))
    # A result at time T must sort before a use at time T (the model cannot have thought for
    # a negative interval); the rank in the tuple enforces that against equal timestamps.
    timeline.sort(key=lambda row: (row[0], row[1]))

    out: list[dict[str, Any]] = []
    last_result: float | None = None
    after_prompt = True                       # nothing precedes the session's first call
    for when, _, item in timeline:
        if item["kind"] == "prompt":
            after_prompt = True
        elif item["kind"] == "result":
            last_result = when
        elif item["kind"] == "use":
            out.append({**item, "ts": when,
                        "seconds": (when - last_result) if last_result is not None else None,
                        "after_prompt": after_prompt})
            after_prompt = False
    return out


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    return values[min(len(values) - 1, int(q * len(values)))]


def summarise(rows: Iterable[dict[str, Any]], *, idle_s: float = IDLE_S,
              match: re.Pattern[str] | None = HARNESS_RE) -> dict[str, Any]:
    """Distribution of think time, with every exclusion counted rather than hidden."""
    rows = list(rows)
    considered = [r for r in rows
                  if match is None or match.search(r.get("command") or "")]
    after_prompt = sum(1 for r in considered if r["after_prompt"])
    timed = [r for r in considered if r["seconds"] is not None and not r["after_prompt"]]
    idle = sum(1 for r in timed if r["seconds"] > idle_s)
    kept = sorted(r["seconds"] for r in timed if r["seconds"] <= idle_s)
    tokens = [r["output_tokens"] for r in considered if r.get("output_tokens")]
    n = len(kept)
    return {
        "calls": len(rows), "matched": len(considered), "n": n,
        "excluded_after_prompt": after_prompt, "excluded_idle": idle,
        "min_s": round(kept[0], 1) if n else 0.0,
        "p50_s": round(_pct(kept, 0.50), 1), "p90_s": round(_pct(kept, 0.90), 1),
        "max_s": round(kept[-1], 1) if n else 0.0,
        "mean_s": round(sum(kept) / n, 1) if n else 0.0,
        "total_s": round(sum(kept), 1),
        "p50_ms": round(_pct(kept, 0.50) * 1000, 1),
        "mean_ms": round(sum(kept) / n * 1000, 1) if n else 0.0,
        "output_tokens_total": sum(tokens),
        "output_tokens_per_step": round(sum(tokens) / len(tokens)) if tokens else 0,
    }


def attach(step_list: list[dict[str, Any]], rows: list[dict[str, Any]], *,
           tolerance_s: float = TOLERANCE_S) -> list[float | None]:
    """Per-step think in ms, matched to journal steps by wall clock.

    A journal `invoke` is written when the run *ends*, so the run began at
    `ts - ms_total`; the transcript's `tool_use` for the same run sits within a spawn's
    distance of that. Matching one-to-one beats a flat average because the expensive steps
    are exactly the ones worth collapsing — an average hides which those were.
    """
    usable = sorted((r for r in rows
                     if r["seconds"] is not None and not r["after_prompt"]),
                    key=lambda r: r["ts"])
    stamps = [r["ts"] for r in usable]
    taken: set[int] = set()
    out: list[float | None] = []
    for step in step_list:
        started = float(step.get("ts") or 0) - float(step.get("ms_total") or 0) / 1000
        idx = bisect.bisect_left(stamps, started)
        best: int | None = None
        for cand in (idx - 1, idx, idx + 1):
            if not 0 <= cand < len(usable) or cand in taken:
                continue
            if abs(stamps[cand] - started) > tolerance_s:
                continue
            if best is None or abs(stamps[cand] - started) < abs(stamps[best] - started):
                best = cand
        if best is None:
            out.append(None)
        else:
            taken.add(best)
            out.append(round(usable[best]["seconds"] * 1000, 1))
    return out


def find(cwd: str | Path | None = None) -> list[Path]:
    """Transcripts for the project containing `cwd`, newest first.

    The project slug comes from the directory Claude Code was *started* in, which is often
    a parent of the working directory (a session opened at `browser-harness/` while working
    in `browser-harness/v2`), so walk upward until a slug matches.
    """
    here = Path(cwd or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        slug = str(candidate).replace("/", "-")
        directory = PROJECTS / slug
        if directory.is_dir():
            found = sorted(directory.glob("*.jsonl"),
                           key=lambda f: f.stat().st_mtime, reverse=True)
            if found:
                return found
    return []
