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

Bulky payloads live in a **content-addressed sidecar** (`<cassette>.blobs/`), with an
`_elide`-shaped marker left in the JSONL. One screenshot response was 51 KB of a 54 KB
session, so the marker keeps the cassette small and diffable — but the Player *reinflates*
markers from the sidecar on delivery, because an elided response handed to the replaying
client is a crash (base64-decoding a digest dict). The marker's shape is deliberately
identical to `_elide`'s output: `signature()` elides live params the same way, so a stored
send and an incoming send hash identically with or without the sidecar.

`diff()` (TODO 28) compares the **send streams** of two cassettes: replay answers requests,
so the requests are the behaviour. A change that turns one round trip into sixty shows up
as a sequence divergence plus a per-method count delta.
"""
from __future__ import annotations

import json
import threading
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any

from harness.core import jsonl
from harness.core.journal import ELIDE_OVER, _elide

SEND = "send"
RECV = "recv"


def _blobs_dir(path: str | Path) -> Path:
    return Path(str(path) + ".blobs")


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
        self._blobs = _blobs_dir(path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text("", encoding="utf-8")

    def _spill(self, value: Any) -> Any:
        """`_elide`'s marker shape, plus the original bytes in the sidecar.

        Content-addressed by the same 16-hex digest the marker carries, so identical
        payloads dedupe to one file and the marker alone names its blob.
        """
        if isinstance(value, str) and len(value) > ELIDE_OVER:
            digest = sha256(value.encode()).hexdigest()[:16]
            try:
                self._blobs.mkdir(parents=True, exist_ok=True)
                blob = self._blobs / digest
                if not blob.exists():
                    blob.write_text(value, encoding="utf-8")
            except OSError:
                pass      # degrade to a plain marker; recording must not break the run
            return {"_elided": len(value), "_sha256": digest}
        if isinstance(value, dict):
            return {k: self._spill(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._spill(v) for v in value]
        return value

    def _log(self, kind: str, msg: dict[str, Any]) -> None:
        line = json.dumps({"t": kind, **self._spill(msg)}, default=str, ensure_ascii=False)
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
        self._blobs = _blobs_dir(path)
        self._load(Path(path))

    def _reinflate(self, value: Any) -> Any:
        """Replace a marker with its sidecar bytes before delivery. An elided response
        handed to the client is a crash (item 28's known break); a missing sidecar
        degrades back to the marker rather than failing the replay."""
        if isinstance(value, dict):
            if set(value) == {"_elided", "_sha256"}:
                try:
                    s = (self._blobs / str(value["_sha256"])).read_text(encoding="utf-8")
                    if len(s) == value["_elided"]:
                        return s
                except OSError:
                    pass
                return value
            return {k: self._reinflate(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._reinflate(v) for v in value]
        return value

    def _load(self, path: Path) -> None:
        """Rebuild exchanges by walking the frames in recorded order.

        Events are attributed to the response they preceded, so replay delivers them in the
        order the client originally observed — an `attachedToTarget` that arrived before its
        `attachToTarget` result must still arrive before it.
        """
        sig_of_id: dict[Any, str] = {}
        pending: list[dict[str, Any]] = []
        for frame in jsonl.read(path, missing_ok=False):
            frame = dict(frame)
            kind, msg = frame.pop("t", None), frame
            if kind == SEND:
                sig_of_id[msg.get("id")] = signature(msg)
            elif kind == RECV and "id" in msg:
                sig = sig_of_id.pop(msg["id"], None)
                if sig is not None:
                    # responses and events reinflate from the sidecar; sends stay elided
                    # because signature() elides the live side identically anyway
                    self._by_sig[sig].append(_Exchange(response=self._reinflate(msg),
                                                       events=pending))
                pending = []
            elif kind == RECV:
                pending.append(self._reinflate(msg))
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


# -- golden-file diff (TODO 28) ---------------------------------------------

def send_signatures(path: str | Path) -> list[str]:
    """The request stream, in order. Replay answers requests, so this IS the behaviour."""
    sigs: list[str] = []
    for frame in jsonl.read(path, missing_ok=False):
        frame = dict(frame)
        if frame.pop("t", None) == SEND:
            sigs.append(signature(frame))
    return sigs


def diff(golden: str | Path, other: str | Path) -> dict[str, Any]:
    """Compare two cassettes' request streams. The per-method count delta is what makes
    "1 round trip became 60" legible at a glance; the first divergence pins where."""
    a, b = send_signatures(golden), send_signatures(other)
    first = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), None)
    if first is None and len(a) != len(b):
        first = min(len(a), len(b))
    ca = Counter(json.loads(s)[0] for s in a)
    cb = Counter(json.loads(s)[0] for s in b)
    deltas = {m: {"golden": ca.get(m, 0), "got": cb.get(m, 0)}
              for m in sorted(set(ca) | set(cb)) if ca.get(m, 0) != cb.get(m, 0)}
    return {"equal": a == b, "golden_calls": len(a), "got_calls": len(b),
            "first_divergence": first, "method_deltas": deltas}
