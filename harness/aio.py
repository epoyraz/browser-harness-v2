"""Async frontends over the same daemon (the answer to "v1 awaits, v2 blocks").

Two independent entry points, one shared architecture:

`AsyncSession` — the whole ops surface (`goto`, `fill_form`, waits, ...) from async code.
Every call runs on this session's own single worker thread. That thread pinning is not an
optimisation; it is what makes the wrapper *correct*: the sync `Session` keeps its current
tab in a thread-local (D1's fix, one level down), so scattering calls across a default
executor pool would lose the cursor between calls. One `AsyncSession` is one cursor, exactly
like one `Session`; parallel callers open several, and the daemon multiplexes them over its
one websocket as always.

`AsyncConnection` — a native asyncio client speaking the daemon's newline-JSON IPC protocol
directly: id-multiplexed requests, typed errors reconstructed from outcome dicts, events
fanned out in-loop. No threads, no bridge — for embedders who want raw CDP-shaped access
without the ops machinery.

Cancellation is **abandonment**, by construction: Python cannot kill a running thread, so a
cancelled `AsyncSession` call finishes in the background and its result is discarded — the
same contract the sync `Connection` already honours for timed-out callers ("a request that
already timed out; its caller has moved on"). `AsyncConnection` cancels cleanly: the pending
future is dropped and a late reply finds no slot.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Self

from harness.connect.cdp import MAX_FRAME
from harness.connect.client import _validate_protocol
from harness.core import ipc
from harness.core.outcome import (
    BrowserDisconnected,
    Class,
    HarnessError,
    Timeout,
    fail,
)

__all__ = ["AsyncConnection", "AsyncSession"]


class _Async:
    """A sync object whose public methods become awaited calls on one pinned executor.

    Used for the session *and* every tab it hands out, so `tab.goto`/`tab.js` — which is
    where navigation and evaluation actually live — work exactly like session methods.
    """

    def __init__(self, obj: Any, executor: ThreadPoolExecutor):
        self._obj = obj
        self._ex = executor

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        attr = getattr(self._obj, name)
        if not callable(attr):
            return attr

        async def call(*args: Any, **kwargs: Any) -> Any:
            return await asyncio.get_running_loop().run_in_executor(
                self._ex, lambda: attr(*args, **kwargs))
        return call


class AsyncSession:
    """`await session.goto(url)` — the full sync `Session`, off the event loop."""

    def __init__(self, sync: Any, executor: ThreadPoolExecutor):
        self._proxy = _Async(sync, executor)
        self._sync = sync
        self._ex = executor

    @classmethod
    async def connect(cls, name: str = "default", **kwargs: Any) -> Self:
        """Spawn/reuse the daemon and open the session, off-loop — on the pinned worker,
        not the default pool: `Session.__init__` can attach a tab (an explicit
        `BH_TARGET_LEASE`, opt-in recording), and the thread-local cursor it sets must
        live on the thread every later op runs on."""
        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bh-aio")

        def build() -> Any:
            from harness.session import Session
            return Session(name, **kwargs)

        sync = await loop.run_in_executor(executor, build)
        return cls(sync, executor)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._proxy, name)

    async def tab(self, target_id: str | None = None) -> _Async:
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(self._ex, lambda: self._sync.tab(target_id))
        return _Async(raw, self._ex)

    async def close(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(self._ex, self._sync.close)
        finally:
            self._ex.shutdown(wait=False)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


class AsyncConnection:
    """Raw CDP over the daemon, natively async. Same wire protocol as `RemoteConnection`.

    Requests and events share one socket; a single reader task demultiplexes by `rid`,
    exactly as the sync pump does. Errors arrive as outcome dicts and are rebuilt into the
    same typed hierarchy via `HarnessError.of`, so callers branch on `Class` identically.
    """

    def __init__(self, name: str, token: str | None, writer: asyncio.StreamWriter):
        self.name = name
        self._token = token
        self._writer = writer
        self._stream: asyncio.StreamReader | None = None
        self._reader: asyncio.Task[None] | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._events: list[Callable[[dict[str, Any]], None]] = []

    @classmethod
    async def connect(cls, name: str = "default", *, timeout: float = 30.0) -> Self:
        loop = asyncio.get_running_loop()
        pong = await loop.run_in_executor(None, lambda: ipc.ping(name))
        if pong is None or not pong.get("browser"):
            # Reuse the sync spawner wholesale: readiness means the browser, not a pong.
            await loop.run_in_executor(None, lambda: _ensure(name))

        def dial() -> tuple[Any, str | None]:
            return ipc.connect(name, timeout=timeout)

        sock, token = await loop.run_in_executor(None, dial)
        sock.setblocking(False)
        # limit must cover the largest frame the daemon can send — a screenshot-bearing
        # CDP reply dwarfs asyncio's 64 KiB default, and an over-limit readline kills
        # the reader task and every pending call with it.
        stream, writer = await asyncio.open_connection(sock=sock, limit=MAX_FRAME)
        conn = cls(name, token, writer)
        conn._stream = stream
        conn._reader = asyncio.create_task(conn._pump(), name="bh-aio-reader")
        subscribed = await conn._call({"meta": "subscribe"}, timeout=10.0)
        _validate_protocol(subscribed.get("value") or {})
        return conn

    def subscribe(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """Register an event handler. Called in-loop; must not block."""
        self._events.append(fn)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def request(self, method: str, params: dict[str, Any] | None = None, *,
                      session_id: str | None = None, timeout: float = 20.0) -> dict[str, Any]:
        payload: dict[str, Any] = {"method": method, "params": params or {},
                                   "timeout": timeout}
        if session_id:
            payload["session_id"] = session_id
        reply = await self._call(payload, timeout=timeout + 5.0)
        if reply.get("ok"):
            return reply.get("value") or {}
        raise HarnessError.of(fail(Class(reply.get("class", Class.CDP_ERROR.value)),
                                   reply.get("detail", ""),
                                   **(reply.get("observed") or {})))

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except (OSError, asyncio.CancelledError):
            pass

    # -- plumbing ----------------------------------------------------------

    async def _call(self, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[rid] = fut
        body = {**payload, "rid": rid}
        if self._token:
            body["token"] = self._token
        try:
            self._writer.write((json.dumps(body, default=str) + "\n").encode())
            await self._writer.drain()
        except (OSError, ConnectionResetError) as e:
            self._pending.pop(rid, None)
            raise BrowserDisconnected(f"daemon went away: {e}") from e
        try:
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:                     # asyncio.TimeoutError aliases the builtin
            self._pending.pop(rid, None)     # a late reply finds no slot, as everywhere
            raise Timeout(
                f"daemon did not answer {payload.get('method') or payload.get('meta')}"
                f" in {timeout}s", daemon=self.name) from None

    async def _pump(self) -> None:
        try:
            assert self._stream is not None
            while line := await self._stream.readline():
                if line.strip():
                    self._dispatch(line)
        except (asyncio.CancelledError, ConnectionResetError, OSError):
            pass
        finally:
            self._fail_all(BrowserDisconnected("daemon connection lost"))

    def _dispatch(self, line: bytes) -> None:
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            return
        if "event" in frame:
            for fn in list(self._events):
                try:
                    fn(frame["event"])
                except Exception:  # noqa: BLE001, S110 — one bad handler must not deafen the rest
                    pass
            return
        rid = frame.pop("rid", None)
        fut = self._pending.pop(rid, None)
        if fut is not None and not fut.done():
            fut.set_result(frame)

    def _fail_all(self, error: BaseException) -> None:
        slots = list(self._pending.values())
        self._pending.clear()
        for fut in slots:
            if not fut.done():
                fut.set_exception(error)


def _ensure(name: str) -> None:
    from harness.connect.client import ensure_daemon
    ensure_daemon(name)
