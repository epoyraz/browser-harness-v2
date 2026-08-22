"""One CDP websocket, id-multiplexed across every client (DESIGN.md D5/D7, TODO 7).

**One connection per browser endpoint, never one per client.** Measured (D7): a second
websocket to an already-authorised Chrome is denied a fresh consent prompt every time —
0 of 6 succeeded — and Chrome serialises the prompts it does show. Per-client connections
would put a modal in front of every subagent. So the daemon holds exactly one, and N
clients multiplex over it.

Threads, not asyncio. The daemon must serve a blocking IPC socket *and* pump CDP frames;
with one reader thread and a pending-request table that is ~40 lines, whereas v1's asyncio
client needed a subclass just to stretch the opening handshake.

`classify()` is the **only** place in the tree that reads CDP's prose. Chrome rewords its
messages between versions, so every reworded string was a new v1 bug — recovery there
string-matched at each call site. Classifying once at the boundary means a rewording
breaks one function with one test, not the whole recovery surface.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable
from typing import Any, Self

from harness.core.journal import Journal
from harness.core.outcome import (
    BrowserDisconnected,
    Class,
    HarnessError,
    Outcome,
    Timeout,
)

#: Chrome's default 1 MB frame cap truncates screenshots and `getFullAXTree` on a real page.
MAX_FRAME = 100 * 1024 * 1024

#: How long the reader parks between liveness checks. Not a request timeout — those are
#: per-call arguments (§6: never an env var).
_PUMP_TICK = 0.5


def classify(error: dict[str, Any]) -> Class:
    """Map a CDP error object onto the closed outcome enum.

    The single prose boundary. Callers branch on the returned `Class`, so Chrome is free to
    reword. An unrecognised error becomes `CDP_ERROR` rather than a guess: rule 1 of the
    outcome contract is never to invent a cause you did not verify, and "the browser refused
    the command, and we do not recognise which way" is a true statement where
    `SESSION_STALE` would be a fabricated one.
    """
    text = str(error.get("message") or "").lower()
    if "session with given id not found" in text or "not attached to an active page" in text:
        return Class.SESSION_STALE
    if "no target with given id" in text or "target closed" in text:
        return Class.TARGET_GONE
    if "target crashed" in text or "renderer" in text and "unresponsive" in text:
        return Class.RENDERER_UNRESPONSIVE
    if "node with given id" in text or "could not find node" in text:
        return Class.ELEMENT_GONE
    if "returned by value" in text or "reference chain is too long" in text:
        return Class.NOT_SERIALIZABLE          # v1 returned None here, silently
    return Class.CDP_ERROR


class WebSocketTransport:
    """The live wire. Translates the websocket library's exceptions into two the rest of
    the tree understands — `TimeoutError` and `EOFError` — so `Connection` never imports
    `websockets` and a cassette can stand in for it unchanged.

    Keepalive pings are **off** (`ping_interval=None`). Measured (repro vs a server that
    never pongs): with the library defaults the connection is force-closed with
    `1011 keepalive ping timeout` ~40 s in — indistinguishable from a real disconnect,
    and here one false positive kills the one multiplexed websocket for every client.
    Real CDP traffic is already our liveness evidence; the reader thread, per-request
    timeouts and lifecycle events own that job (the same root cause as cdp-use PR #25 /
    browser-use#4688 "random mid-session drops"). Callers who want the transport-level
    check back can pass explicit values.
    """

    def __init__(
        self,
        url: str,
        *,
        open_timeout: float = 30.0,
        ping_interval: float | None = None,
        ping_timeout: float = 20.0,
    ):
        from websockets.sync.client import connect

        self.url = url
        self._ws = connect(
            url,
            max_size=MAX_FRAME,
            open_timeout=open_timeout,
            ping_interval=ping_interval,
            ping_timeout=ping_timeout,
        )

    def send(self, msg: dict[str, Any]) -> None:
        from websockets.exceptions import ConnectionClosed

        try:
            self._ws.send(json.dumps(msg, default=str))
        except ConnectionClosed as e:
            raise EOFError(f"browser closed the connection: {e}") from e

    def recv(self, timeout: float | None = None) -> dict[str, Any]:
        from websockets.exceptions import ConnectionClosed

        try:
            return json.loads(self._ws.recv(timeout=timeout))
        except ConnectionClosed as e:
            raise EOFError(f"browser closed the connection: {e}") from e

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001, S110 — teardown must not mask the real failure
            pass


class _Slot:
    """One in-flight request, waiting on the reader thread."""

    __slots__ = ("done", "error", "result")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: dict[str, Any] | None = None
        self.error: BaseException | None = None


class Connection:
    """One websocket; every request is routed back to the thread that made it.

    Safe to call `request()` from many threads at once — that is the point. Ordering is
    per-caller, not global: two clients driving two tabs do not queue behind each other.
    """

    def __init__(self, transport: Any | None = None, *, journal: Journal | None = None):
        self._t = transport
        self._j = journal or Journal(None)
        self._pending: dict[int, _Slot] = {}
        self._subscribers: list[Callable[[dict[str, Any]], None]] = []
        self._lock = threading.Lock()
        self._next_id = 0
        self._closed = False
        #: `_closed` is now written by the reader too, so it can no longer double as the
        #: "teardown already ran" latch `close()` used it for. Separating them keeps a
        #: browser that dropped its socket from short-circuiting `close()` and leaving the
        #: transport's fd open for the life of the daemon.
        self._torn_down = False
        self._reader: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def attach(self, transport: Any) -> None:
        """Install the transport after construction.

        `WebSocketTransport.__init__` *is* the handshake, and Chrome blocks it behind a
        consent prompt. The daemon must publish its IPC endpoint before paying that cost,
        which means the Connection has to exist before there is a websocket to give it.
        """
        self._t = transport

    def start(self) -> Self:
        if self._t is None:
            raise BrowserDisconnected("connection has no transport; call attach() first")
        reader = threading.Thread(target=self._pump, name="cdp-reader", daemon=True)
        reader.start()
        # Published only after `start()` returns: `Thread.is_alive()` is False *before* a
        # thread starts as well as after it ends, and `closed` reads that flag. Assigning
        # first would make a connection look dead for the microseconds between the two.
        self._reader = reader
        return self

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._torn_down:
                return
            self._torn_down = True
        self._t.close()
        self._fail_pending(BrowserDisconnected("connection closed locally"))

    @property
    def closed(self) -> bool:
        """Whether this connection can still carry a request. Truthful after the browser
        goes away, which it was not: `_closed` used to be written only by `close()`, so a
        Chrome that quit or crashed left this object reporting itself open — and the daemon
        above went on advertising `browser: true` over it."""
        with self._lock:
            return self._dead()

    def _dead(self) -> bool:
        """Callers must hold `_lock`.

        Two signals, because one is not enough. `_closed` is authoritative — `close()` and
        every terminal path in `_pump` set it. The reader-liveness check behind it covers
        what `_pump` cannot: a thread that ended somewhere no `except` of ours runs. A
        connection with no reader can never complete a request, so reporting it open is the
        exact v1 "healthy corpse" this liveness path exists to prevent.
        """
        if self._closed:
            return True
        return self._reader is not None and not self._reader.is_alive()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False

    # -- events ------------------------------------------------------------

    def subscribe(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """Register an event handler. Handlers run on the reader thread and must not block —
        and must never call `request()`: the reader cannot dispatch the reply it would be
        waiting for, so that is a deadlock by construction."""
        with self._lock:
            self._subscribers.append(fn)

    def unsubscribe(self, fn: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            if fn in self._subscribers:
                self._subscribers.remove(fn)

    @property
    def journal(self) -> Journal:
        return self._j

    # -- requests ----------------------------------------------------------

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        """One CDP round trip. Returns `result`, or raises the typed error for its class.

        `timeout` is an argument, never an env var (§6). v1's single global `BH_IPC_TIMEOUT`
        had to cover both a 5 ms `DOM.getBoxModel` and a 90 s in-page fetch, so whichever
        value was chosen was wrong for one of them.
        """
        with self._lock:
            if self._dead():
                raise BrowserDisconnected("connection is closed", method=method)
            self._next_id += 1
            msg_id = self._next_id
            slot = _Slot()
            self._pending[msg_id] = slot

        msg: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id

        marker = self._j.cdp_start(method, params)
        try:
            self._t.send(msg)
        except EOFError as e:
            self._drop(msg_id)
            error = BrowserDisconnected(str(e), method=method)
            self._j.cdp_end(marker, method, error=error)
            raise error from e

        if not slot.done.wait(timeout):
            self._drop(msg_id)
            error = Timeout(
                f"{method} did not answer in {timeout}s",
                method=method, session=session_id, timeout=timeout,
            )
            self._j.cdp_end(marker, method, error=error)
            raise error
        if slot.error is not None:
            self._j.cdp_end(marker, method, error=slot.error)
            raise slot.error
        result = slot.result or {}
        self._j.cdp_end(marker, method, result=result)
        return result

    def _drop(self, msg_id: int) -> None:
        with self._lock:
            self._pending.pop(msg_id, None)

    # -- reader ------------------------------------------------------------

    def _pump(self) -> None:
        while True:
            with self._lock:
                if self._closed:
                    return
            try:
                msg = self._t.recv(timeout=_PUMP_TICK)
            except TimeoutError:
                continue
            except EOFError as e:
                self._reader_stopped(BrowserDisconnected(str(e)))
                return
            except Exception as e:  # noqa: BLE001 — a dead reader must wake its waiters
                self._reader_stopped(BrowserDisconnected(f"reader failed: {e}"))
                return
            try:
                self._dispatch(msg)
            except Exception as e:  # noqa: BLE001 — the third terminal path
                # `_dispatch` used to sit outside the try, so a malformed frame (a JSON
                # array, say, where `msg.get` is an AttributeError) killed the reader with
                # `_closed` still False and `_pending` never drained — every in-flight
                # caller then waited out its FULL per-call timeout. That is precisely the
                # failure the two branches above exist to prevent, reachable by a third
                # door. The next call recovers via the `is_alive()` fallback; the calls
                # already waiting do not, which is why this has to claim the connection.
                self._reader_stopped(BrowserDisconnected(f"reader failed in dispatch: {e}"))
                return

    def _reader_stopped(self, error: BrowserDisconnected) -> None:
        """The reader is gone; the connection goes with it.

        Neither terminal path used to mark the connection closed, and `_closed` was written
        nowhere else but `close()`. So after Chrome quit, crashed or dropped the socket, the
        reader thread was gone while the object still called itself open: every later call
        wrote into the void and waited out its full per-call timeout, and `Daemon._meta`'s
        ping kept answering `browser: true` — the healthy-corpse report v1 needed six PRs to
        stop making. Closing here is what makes `closed` (and the pong above it) truthful.
        """
        self._j.write("note", msg="cdp reader stopped", error=str(error))
        self._fail_pending(error, closing=True)

    def _dispatch(self, msg: dict[str, Any]) -> None:
        if "id" in msg:
            with self._lock:
                slot = self._pending.pop(msg["id"], None)
            if slot is None:
                return          # a request that already timed out; its caller has moved on
            if "error" in msg:
                err = msg["error"] or {}
                cls = classify(err)
                slot.error = HarnessError.of(Outcome(
                    ok=False, cls=cls,
                    detail=str(err.get("message") or "")[:300],
                    observed={"code": err.get("code"), "data": err.get("data")},
                ))
            else:
                slot.result = msg.get("result") or {}
            slot.done.set()
            return
        # An event. Subscribers are notified in registration order; the session registry
        # subscribes first so staleness is recorded before anyone else reacts to it.
        with self._lock:
            subscribers = list(self._subscribers)
        for fn in subscribers:
            try:
                fn(msg)
            except Exception as e:  # noqa: BLE001 — one bad handler must not kill the reader
                self._j.write("note", msg=f"event handler failed: {e}", event=msg.get("method"))

    def _fail_pending(self, error: BaseException, *, closing: bool = False) -> None:
        """Wake every waiter. A reader that dies silently turns each in-flight call into a
        full-timeout hang, which is how a dropped connection used to read as slowness.

        `closing` folds the `_closed` write into the same critical section as the drain, so
        the flag is set before any waiter is woken and a concurrent `request()` cannot slip
        a fresh slot into `_pending` between the two — that one would have waited out its
        full timeout against a reader that is already gone.
        """
        with self._lock:
            if closing:
                self._closed = True
            slots = list(self._pending.values())
            self._pending.clear()
        for slot in slots:
            slot.error = error
            slot.done.set()
