"""Append-only session journal (DESIGN.md D11b).

One artifact, three readers: `--trace` renders it, forensics orders it, `replay` executes
it. v1 had none of the three — its daemon log was written as `f"{msg}\\n"` with no
timestamp, no request id and no pid, so a stale-session reattach could not be ordered
against any client call. That is why "did the harness navigate the user's tab?" was
unanswerable from the record.

Four fields make one file serve all three purposes:

  ts    when          — orders entries against each other
  id    which call    — the SAME id appears on the client entry and the daemon entry,
                        which is the correlation v1 could not do
  kind  what sort     — invoke | call | cdp | daemon | note
  ...   the payload

Design constraints, each earned:
  - Writes never raise. Observability that can break the run is worse than none.
  - Success is silent; the journal is written regardless, but nothing prints unless asked.
  - Screenshot payloads are elided by digest — 92% of a cassette's bytes were one image.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

#: Payloads above this are replaced by {"_elided": n, "_sha256": "..."}.
#: A single screenshot response was 51 KB of a 54 KB session.
ELIDE_OVER = 2048


def _elide(value: Any) -> Any:
    """Replace bulky leaf strings with a digest so the journal stays diffable.

    A digest is enough for replay: a cassette compares *what was requested*, and an
    identical image hashes identically.
    """
    if isinstance(value, str) and len(value) > ELIDE_OVER:
        return {"_elided": len(value), "_sha256": sha256(value.encode()).hexdigest()[:16]}
    if isinstance(value, dict):
        return {k: _elide(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_elide(v) for v in value]
    return value


@dataclass(slots=True)
class Span:
    """An in-flight call. Closed by `Journal.call()`'s context manager."""

    id: str
    fn: str
    started: float
    cdp_calls: int = 0

    @property
    def ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000


class Journal:
    """Append-only JSONL. Thread-safe; every write is one line, flushed."""

    def __init__(self, path: str | os.PathLike[str] | None, *, session: str = ""):
        self.path = Path(path) if path else None
        self.session = session or f"s{int(time.time())}"
        self._n = 0
        self._lock = threading.Lock()
        self._stack: list[Span] = []
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.path = None          # never let observability break the run

    # -- writing ----------------------------------------------------------

    def write(self, kind: str, *, id: str = "", **payload: Any) -> None:
        """Append one entry. Never raises."""
        if not self.path:
            return
        entry = {"ts": round(time.time(), 3), "id": id or self.session, "kind": kind,
                 **_elide(payload)}
        line = json.dumps(entry, default=str, ensure_ascii=False)
        try:
            with self._lock, self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:  # noqa: BLE001, S110 — deliberate: see the "never raise" rule above.
            # Blind rather than enumerated, on purpose. Narrowing to OSError would let a
            # novel serialisation error take down a run for the sake of a log line.
            pass

    def next_id(self) -> str:
        with self._lock:
            self._n += 1
            return f"{self.session}.{self._n}"

    # -- spans ------------------------------------------------------------

    def call(self, fn: str, **args: Any) -> _CallCtx:
        """Context manager recording one helper call and its outcome.

        The id it allocates is what the daemon echoes back, so a client failure and a
        daemon log line can finally be joined.
        """
        return _CallCtx(self, fn, args)

    def cdp(self, method: str) -> None:
        """Count a CDP round trip against the innermost open span.

        The round-trip count is the actionable number: it is what makes a 20-character
        fill costing 61 round trips visible without a benchmark.
        """
        if self._stack:
            self._stack[-1].cdp_calls += 1

    @property
    def current(self) -> Span | None:
        return self._stack[-1] if self._stack else None

    # -- reading ----------------------------------------------------------

    def entries(self) -> Iterator[dict[str, Any]]:
        """Read back. A truncated final line is skipped, not fatal."""
        if not self.path or not self.path.exists():
            return
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


class _CallCtx:
    def __init__(self, journal: Journal, fn: str, args: dict[str, Any]):
        self.j, self.fn, self.args = journal, fn, args
        self.span: Span | None = None

    def __enter__(self) -> Span:
        self.span = Span(id=self.j.next_id(), fn=self.fn, started=time.perf_counter())
        self.j._stack.append(self.span)
        return self.span

    def __exit__(self, exc_type, exc, tb) -> bool:
        span = self.span
        assert span is not None
        self.j._stack.pop()
        payload: dict[str, Any] = {"fn": self.fn, "args": self.args,
                                   "ms": round(span.ms, 1), "cdp": span.cdp_calls}
        if exc is not None:
            # rule 2: never discard a cause you were handed
            outcome = getattr(exc, "outcome", None)
            payload["outcome"] = (outcome.to_json() if outcome is not None
                                  else {"ok": False, "class": type(exc).__name__,
                                        "detail": str(exc)[:200]})
        else:
            payload["outcome"] = {"ok": True}
        self.j.write("call", id=span.id, **payload)
        return False          # never swallow
