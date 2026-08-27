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
  - Screenshot payloads are elided by digest — 92% of a trace's bytes were one image.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from harness.core import jsonl
from harness.core.content import DEFAULT_OUTPUT_BYTES, ContentRef, ContentStore

#: Payloads above this are replaced by {"_elided": n, "_sha256": "..."}.
#: A single screenshot response was 51 KB of a 54 KB session.
ELIDE_OVER = 2048

# Correlation is deliberately an allow-list.  A caller cannot accidentally put a URL,
# form answer, selector, or JavaScript source onto every protocol event by binding an
# arbitrary dictionary.
CONTEXT_KEYS = frozenset({
    "task_id", "item_id", "item_index", "worker_id", "target_id",
    "browser_context_id", "stage", "hop", "run_id",
    # Mechanical observability attribution only.  Values are fixed by harness internals;
    # no page data crosses this allow-list.
    "observability", "recording_span_id",
})

_SUMMARY_KEYS = frozenset({
    "fn", "ms", "cdp", "parent", "event", "method", "ok", "error_class",
    "error_code", "frame", "frame_suppressed", "recording_profile",
})


def _elide(value: Any) -> Any:
    """Replace bulky leaf strings with a digest so the journal stays diffable.

    A digest is enough to compare runs: what matters is *what was requested*, and an
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

    def __init__(self, path: str | os.PathLike[str] | None, *, session: str = "",
                 cdp_origin: str = "", content_store: ContentStore | None = None,
                 entry_limit: int = DEFAULT_OUTPUT_BYTES):
        #: Called as the span closes, before its entry is written, with (span, payload).
        #: Whatever it returns is merged into the entry — which is how a recording adds
        #: `frame` to the very call it belongs to instead of writing a second file that
        #: has to be joined back later (v1 kept a parallel events.jsonl for exactly this
        #: and paid for the duplication).
        self.on_call: Callable[[Span, dict[str, Any]], dict[str, Any] | None] | None = None
        self.path = Path(path) if path else None
        self.session = session or f"s{int(time.time())}"
        self.cdp_origin = cdp_origin
        self.content_store = content_store
        self.entry_limit = max(0, int(entry_limit))
        self.trace_cdp = os.environ.get("BH_CDP_TRACE", "").strip().lower() \
            not in ("", "0", "false", "no")
        self._n = 0
        self._lock = threading.Lock()
        # Per-thread, because span nesting is a property of one call chain, not of the
        # process. With `parallel()` driving several tabs at once a shared stack would
        # record thread A's js() as a child of thread B's goto(), and `cdp()` would bill
        # round trips to whichever span happened to be globally innermost — turning the
        # trace tree and the round-trip counts into fiction exactly when they matter most.
        self._local = threading.local()
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.path = None          # never let observability break the run

    # -- writing ----------------------------------------------------------

    @staticmethod
    def _journal_marker(ref: ContentRef) -> dict[str, Any]:
        """A reversible marker with no content preview.

        Journal payloads are deliberately less revealing than agent-facing output: even a
        head/tail preview could leak page text, headers, or a form value into a long-lived
        trace. Byte/type metadata and the private-cache digest are enough here.
        """
        marker = ref.marker(surface="journal")
        marker.pop("head", None)
        marker.pop("tail", None)
        marker["preview"] = "omitted_for_journal_privacy"
        return marker

    def _shape(self, value: Any) -> Any:
        """Recursively make large leaves reversible when this journal has a store."""
        if isinstance(value, str) and len(value) > ELIDE_OVER:
            if self.content_store is not None:
                try:
                    return self._journal_marker(self.content_store.put(value))
                except Exception:  # noqa: BLE001 — observability still must not break work
                    return _elide(value)
            return _elide(value)
        if isinstance(value, dict):
            return {key: self._shape(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._shape(item) for item in value]
        return value

    @staticmethod
    def _summary(entry: dict[str, Any]) -> dict[str, Any]:
        summary = {key: entry[key] for key in _SUMMARY_KEYS if key in entry}
        outcome = entry.get("outcome")
        if isinstance(outcome, dict):
            summary["outcome"] = {key: outcome[key] for key in ("ok", "class")
                                  if key in outcome}
        return summary

    def write(self, kind: str, *, id: str = "", **payload: Any) -> None:
        """Append one entry. Never raises."""
        if not self.path:
            return
        base = {"ts": round(time.time(), 3), "id": id or self.session, "kind": kind,
                **self.context}
        raw = {**base, **payload}
        try:
            entry = self._shape(raw)
            line = json.dumps(entry, default=str, ensure_ascii=False)
            if len(line.encode("utf-8")) > self.entry_limit:
                if self.content_store is not None:
                    try:
                        ref = self.content_store.put(raw)
                        overflow = self._journal_marker(ref)
                    except Exception:  # noqa: BLE001 — degrade without breaking the run
                        overflow = None
                else:
                    overflow = None
                if overflow is None:
                    encoded = json.dumps(raw, default=str, ensure_ascii=False).encode("utf-8")
                    overflow = {
                        "_elided": len(encoded),
                        "_sha256": sha256(encoded).hexdigest()[:16],
                        "preview": "omitted_for_journal_privacy",
                    }
                entry = {**base, **self._summary(raw), "entry_overflow": overflow}
                line = json.dumps(self._shape(entry), default=str, ensure_ascii=False)
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

    @contextmanager
    def call(self, fn: str, **args: Any) -> Iterator[Span]:
        """Context manager recording one helper call and its outcome.

        The id it allocates is what the daemon echoes back, so a client failure and a
        daemon log line can finally be joined.
        """
        span = Span(id=self.next_id(), fn=fn, started=time.perf_counter())
        self._stack.append(span)
        error: BaseException | None = None
        try:
            yield span
        except BaseException as caught:
            error = caught
            raise
        finally:
            self._stack.pop()
            payload: dict[str, Any] = {
                "fn": fn, "args": args, "ms": round(span.ms, 1), "cdp": span.cdp_calls,
            }
            if self._stack:
                payload["parent"] = self._stack[-1].id
            if error is None:
                payload["outcome"] = {"ok": True}
            else:
                outcome = getattr(error, "outcome", None)
                payload["outcome"] = (outcome.to_json() if outcome is not None else {
                    "ok": False, "class": type(error).__name__, "detail": str(error)[:200],
                })
            if self.on_call is not None:
                try:
                    extra = self.on_call(span, payload)
                except Exception:  # noqa: BLE001 — observability must never break the run
                    extra = None
                if extra:
                    payload.update(extra)
            self.write("call", id=span.id, **payload)

    @property
    def _stack(self) -> list[Span]:
        """This thread's open spans. Created lazily: threading.local has no per-thread
        default, and a worker thread must start with an empty stack, not inherit one."""
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = self._local.stack = []
        return stack

    @property
    def context(self) -> dict[str, Any]:
        """Privacy-safe correlation fields bound to this worker thread."""
        return dict(getattr(self._local, "context", {}))

    @contextmanager
    def bind(self, **fields: Any) -> Iterator[None]:
        """Attach safe task/stage identity to calls and CDP events in this thread.

        Bindings nest and restore exactly, so a hop can add ``stage`` without losing the
        worker and item identity installed by :func:`parallel`.
        """
        previous = self.context
        current = dict(previous)
        current.update({key: value for key, value in fields.items()
                        if key in CONTEXT_KEYS and value is not None})
        self._local.context = current
        try:
            yield
        finally:
            self._local.context = previous

    def cdp(self, method: str) -> None:
        """Count a CDP round trip against the innermost open span.

        The round-trip count is the actionable number: it is what makes a 20-character
        fill costing 61 round trips visible without a benchmark.
        """
        if self._stack:
            self._stack[-1].cdp_calls += 1

    def cdp_start(self, method: str, params: dict[str, Any] | None = None) \
            -> tuple[str, float, str | None, int, list[str]] | None:
        """Count a round trip and optionally begin a sanitized protocol event.

        Values are deliberately excluded: CDP parameters can contain cookies, form
        answers, JavaScript source, uploaded paths, and screenshot bytes.  Method names,
        keys, sizes, timing, and parent spans are enough for a Protocol-Monitor-style
        explorer without turning observability into a credential store.
        """
        self.cdp(method)
        if not self.trace_cdp:
            return None
        parent = self.current.id if self.current is not None else None
        encoded = json.dumps(params or {}, default=str, ensure_ascii=False).encode()
        return (self.next_id(), time.perf_counter(), parent, len(encoded),
                sorted(str(k) for k in (params or {})))

    def cdp_end(self, marker: tuple[str, float, str | None, int, list[str]] | None,
                method: str, *, result: Any = None, error: BaseException | None = None) -> None:
        if marker is None:
            return
        event_id, started, parent, request_bytes, param_keys = marker
        try:
            response_bytes = len(json.dumps(
                result, default=str, ensure_ascii=False).encode()) if result is not None else 0
        except (OverflowError, RecursionError, TypeError, ValueError):
            response_bytes = 0
        payload: dict[str, Any] = {
            "method": method, "ms": round((time.perf_counter() - started) * 1000, 1),
            "request_bytes": request_bytes, "response_bytes": response_bytes,
            "param_keys": param_keys, "ok": error is None,
        }
        if self.cdp_origin:
            payload["origin"] = self.cdp_origin
        if parent:
            payload["parent"] = parent
        if isinstance(result, dict):
            payload["result_keys"] = sorted(str(k) for k in result)
        if error is not None:
            outcome = getattr(error, "outcome", None)
            detail = str(getattr(outcome, "detail", "") or "")
            observed = getattr(outcome, "observed", {}) or {}
            payload["error_class"] = (
                getattr(getattr(outcome, "cls", None), "value", None)
                or type(error).__name__
            )
            if observed.get("code") is not None:
                payload["error_code"] = observed["code"]
            if detail:
                payload["error_detail_sha256"] = sha256(detail.encode()).hexdigest()[:16]
        self.write("cdp", id=event_id, **payload)

    @property
    def current(self) -> Span | None:
        return self._stack[-1] if self._stack else None

    # -- reading ----------------------------------------------------------

    def entries(self) -> Iterator[dict[str, Any]]:
        """Read back. A truncated final line is skipped, not fatal."""
        if not self.path or not self.path.exists():
            return
        yield from jsonl.read(self.path)
