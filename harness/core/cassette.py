"""CDP cassette record/replay (DESIGN.md D11c, TODO 27).

Pulled ahead of its phase because items 7–10 need it. The session registry is the part v1
got wrong four separate times, and testing it against live Chrome is both non-deterministic
and expensive: a second connection to an already-authorised Chrome costs a consent prompt
every time (D7). A cassette makes the session logic testable from its first commit rather
than retrofitted afterwards.

Frames are keyed by **request signature, never by message id**. Ids are assigned by the
client and shift the moment a code path changes, so an id-keyed cassette would replay a
different run's answers into the same slots and still look green. The signature — method,
sessionId, params — is what the run actually asked for.

That is also the mechanism behind `bh replay --diff` (TODO 28): a change that turns one
round trip into sixty stops matching, and the cassette misses instead of quietly passing.

Payloads over `ELIDE_OVER` become a digest. One screenshot response was 51 KB of a 54 KB
session, and a digest still compares equal across runs, which is all replay needs.
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from harness.core.journal import _elide

SEND = "send"
RECV = "recv"


class CassetteMiss(KeyError):
    """The run asked for something the cassette never recorded.

    Deliberately not a `Class` member: a miss is a *test* result, not a browser outcome.
    Folding it into the outcome enum would let a recording gap masquerade as a page failure.
    """


def signature(msg: dict[str, Any]) -> str:
    """What a request asked for, independent of when it was asked.

    `sessionId` is part of the identity — the same `Runtime.evaluate` against two tabs is
    two different requests, and conflating them is how a replayed test can pass while
    driving the wrong target.
    """
    return json.dumps(
        [msg.get("method"), msg.get("sessionId"), _elide(msg.get("params") or {})],
        sort_keys=True, default=str,
    )


@dataclass(slots=True)
class _Exchange:
    """One recorded response, plus the events that arrived ahead of it."""

    response: dict[str, Any]
    events: list[dict[str, Any]] = field(default_factory=list)


class Recorder:
    """Transport wrapper that writes every frame to a cassette. Never alters traffic.

    Wrapping rather than subclassing keeps recording orthogonal to which transport is
    underneath, so a cassette can be captured from a live websocket or from another
    cassette while re-recording.
    """

    def __init__(self, inner: Any, path: str | Path):
        self._inner = inner
        self._path = Path(path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text("", encoding="utf-8")

    def _log(self, kind: str, msg: dict[str, Any]) -> None:
        line = json.dumps({"t": kind, **_elide(msg)}, default=str, ensure_ascii=False)
        try:
            with self._lock, self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass          # recording must never break the run it is recording

    def send(self, msg: dict[str, Any]) -> None:
        self._log(SEND, msg)
        self._inner.send(msg)

    def recv(self, timeout: float | None = None) -> dict[str, Any]:
        msg = self._inner.recv(timeout=timeout)
        self._log(RECV, msg)
        return msg

    def close(self) -> None:
        self._inner.close()


class Player:
    """A transport that answers from a cassette. Touches no network.

    Interface-compatible with `WebSocketTransport`: `send` / `recv(timeout)` / `close`,
    raising `TimeoutError` when nothing is pending and `EOFError` once closed.
    """

    def __init__(self, path: str | Path):
        self._by_sig: dict[str, deque[_Exchange]] = defaultdict(deque)
        self._tail: deque[dict[str, Any]] = deque()
        self._queue: deque[dict[str, Any]] = deque()
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._closed = False
        self._load(Path(path))

    def _load(self, path: Path) -> None:
        """Rebuild exchanges by walking the frames in recorded order.

        Events are attributed to the response they preceded, so replay delivers them in the
        order the client originally observed — an `attachedToTarget` that arrived before its
        `attachToTarget` result must still arrive before it.
        """
        sig_of_id: dict[Any, str] = {}
        pending: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue          # a run killed mid-write must not poison the whole cassette
            kind, msg = frame.pop("t", None), frame
            if kind == SEND:
                sig_of_id[msg.get("id")] = signature(msg)
            elif kind == RECV and "id" in msg:
                sig = sig_of_id.pop(msg["id"], None)
                if sig is not None:
                    self._by_sig[sig].append(_Exchange(response=msg, events=pending))
                pending = []
            elif kind == RECV:
                pending.append(msg)
        self._tail.extend(pending)          # events after the last response

    def send(self, msg: dict[str, Any]) -> None:
        sig = signature(msg)
        with self._lock:
            queue = self._by_sig.get(sig)
            if not queue:
                raise CassetteMiss(self._explain(msg, sig))
            exchange = queue.popleft()
            self._queue.extend(exchange.events)
            # The id is the client's, not the recording's; everything else is verbatim.
            self._queue.append({**exchange.response, "id": msg.get("id")})
            self._ready.set()

    def _explain(self, msg: dict[str, Any], sig: str) -> str:
        """A miss is usually a changed request, so name what did change."""
        method = msg.get("method")
        same_method = [s for s in self._by_sig if json.loads(s)[0] == method and self._by_sig[s]]
        if not same_method:
            return f"cassette has no {method!r} left; asked for {sig}"
        return (f"{method!r} was recorded with different params/session. "
                f"asked: {sig}  recorded: {same_method[0]}")

    def recv(self, timeout: float | None = None) -> dict[str, Any]:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                if self._queue:
                    return self._queue.popleft()
                if self._tail:
                    return self._tail.popleft()
                if self._closed:
                    raise EOFError("cassette player closed")
                self._ready.clear()
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("no recorded frame pending")
            wait = 0.05 if deadline is None else max(0.0, deadline - time.monotonic())
            self._ready.wait(min(wait, 0.05))

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._ready.set()

    @property
    def exhausted(self) -> bool:
        """True when every recorded exchange has been replayed.

        A test that finishes green with exchanges left over usually took a shorter path
        than the recording did, which is worth failing on.
        """
        return not any(self._by_sig.values())
