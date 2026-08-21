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
import weakref
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Self

from harness.connect.client import _validate_protocol
from harness.connect.daemon import MAX_DAEMON_FRAME
from harness.core import ipc
from harness.core.outcome import (
    BrowserDisconnected,
    Class,
    HarnessError,
    ProtocolMismatch,
    Timeout,
    fail,
)

__all__ = ["AsyncConnection", "AsyncSession"]

def _wrap_result(value: Any, executor: ThreadPoolExecutor) -> Any:
    """Keep every ``Tab`` returned by a session operation on the pinned worker."""
    from harness.ops.page import Tab

    return _Async(value, executor) if isinstance(value, Tab) else value


def _schedule_session_close(sync: Any, executor: ThreadPoolExecutor) -> None:
    """Best-effort non-blocking cleanup used by GC and cancelled ``connect()`` calls."""
    try:
        executor.submit(sync.close)
    except RuntimeError:
        pass
    finally:
        # Queued/running work still completes with wait=False; no new work is accepted.
        executor.shutdown(wait=False)


def _close_built_session(future: Future[Any], executor: ThreadPoolExecutor) -> None:
    """Close a Session whose construction outlived a cancelled async connect."""
    try:
        sync = future.result()
    except Exception:  # noqa: BLE001 - constructor failure only decides executor cleanup
        executor.shutdown(wait=False)
    else:
        _schedule_session_close(sync, executor)


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
            value = await asyncio.get_running_loop().run_in_executor(
                self._ex, lambda: attr(*args, **kwargs))
            return _wrap_result(value, self._ex)
        return call


class AsyncSession:
    """`await session.goto(url)` — the full sync `Session`, off the event loop."""

    def __init__(self, sync: Any, executor: ThreadPoolExecutor,
                 namespace: dict[str, Any] | None = None):
        self._proxy = _Async(sync, executor)
        self._sync = sync
        self._ex = executor
        self._namespace = namespace or {}
        self._closed = False
        self._finalizer = weakref.finalize(self, _schedule_session_close, sync, executor)

    @classmethod
    async def connect(cls, name: str = "default", **kwargs: Any) -> Self:
        """Spawn/reuse the daemon and open the session, off-loop — on the pinned worker,
        not the default pool: `Session.__init__` can attach a tab (an explicit
        `BH_TARGET_LEASE`, opt-in recording), and the thread-local cursor it sets must
        live on the thread every later op runs on."""
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bh-aio")

        def build() -> Any:
            from harness.session import Session
            return Session(name, **kwargs)

        # A concurrent future gives cancellation a cleanup hook that runs even if the
        # event loop closes before Session.__init__ returns. Shielding prevents asyncio
        # from cancelling the underlying construction and losing the resulting Session.
        built = executor.submit(build)
        try:
            sync = await asyncio.shield(asyncio.wrap_future(built))
        except asyncio.CancelledError:
            built.add_done_callback(lambda future: _close_built_session(future, executor))
            raise
        except BaseException:
            executor.shutdown(wait=False)
            raise

        try:
            # This is the actual synchronous public surface used by `bh` scripts: it
            # contains current-tab operations (`goto`, waits), form helpers, extensions,
            # and Session methods. Build it on the pinned thread for the same reason as
            # the Session itself.
            namespace = await asyncio.get_running_loop().run_in_executor(
                executor, sync.namespace)
        except BaseException:
            _schedule_session_close(sync, executor)
            raise
        return cls(sync, executor, namespace)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name == "session":
            return self
        if name in self._namespace:
            attr = self._namespace[name]
            if isinstance(attr, type) or not callable(attr):
                return attr

            async def call(*args: Any, **kwargs: Any) -> Any:
                value = await asyncio.get_running_loop().run_in_executor(
                    self._ex, lambda: attr(*args, **kwargs))
                return _wrap_result(value, self._ex)
            return call
        return getattr(self._proxy, name)

    async def tab(self, target_id: str | None = None) -> _Async:
        loop = asyncio.get_running_loop()
        raw = await loop.run_in_executor(self._ex, lambda: self._sync.tab(target_id))
        return _wrap_result(raw, self._ex)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._finalizer.detach()
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
        self._closed = False

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
        # _pump reads fixed-size chunks rather than StreamReader.readline(), so a normal
        # screenshot or AX tree isn't constrained by asyncio's 64 KiB line limit.
        stream, writer = await asyncio.open_connection(sock=sock)
        conn = cls(name, token, writer)
        conn._stream = stream
        conn._reader = asyncio.create_task(conn._pump(), name="bh-aio-reader")
        try:
            subscribed = await conn._call({"meta": "subscribe"}, timeout=10.0)
            _validate_protocol(subscribed.get("value") or {})
        except BaseException:
            await conn.close()
            raise
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
        if self._closed:
            return
        self._closed = True
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except OSError:
            pass
        finally:
            if reader is not None:
                await asyncio.gather(reader, return_exceptions=True)

    # -- plumbing ----------------------------------------------------------

    async def _call(self, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        if self._closed:
            raise BrowserDisconnected("client connection is closed")
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
            try:
                return await asyncio.wait_for(fut, timeout)
            except TimeoutError:                 # asyncio.TimeoutError aliases the builtin
                raise Timeout(
                    f"daemon did not answer {payload.get('method') or payload.get('meta')}"
                    f" in {timeout}s", daemon=self.name) from None
        except (OSError, ConnectionResetError) as e:
            raise BrowserDisconnected(f"daemon went away: {e}") from e
        finally:
            # Covers success, timeout, cancellation during drain/wait, and transport
            # failure. A late reply therefore always finds no abandoned slot.
            self._pending.pop(rid, None)

    async def _pump(self) -> None:
        failure: BaseException = BrowserDisconnected("daemon connection lost")
        buf = bytearray()
        try:
            assert self._stream is not None
            while chunk := await self._stream.read(1 << 16):
                buf.extend(chunk)
                while (newline := buf.find(b"\n")) >= 0:
                    line = bytes(buf[:newline])
                    del buf[:newline + 1]
                    if len(line) > MAX_DAEMON_FRAME:
                        raise ProtocolMismatch(
                            f"daemon frame exceeds {MAX_DAEMON_FRAME} bytes")
                    if line.strip():
                        self._dispatch(line)
                if len(buf) > MAX_DAEMON_FRAME:
                    raise ProtocolMismatch(
                        f"daemon frame exceeds {MAX_DAEMON_FRAME} bytes")
            if buf.strip():
                raise ProtocolMismatch("daemon closed in the middle of a JSON frame")
        except asyncio.CancelledError:
            raise
        except ProtocolMismatch as error:
            failure = error
        except (ConnectionResetError, OSError) as error:
            failure = BrowserDisconnected(f"daemon connection lost: {error}")
        except (TypeError, ValueError) as error:
            failure = ProtocolMismatch(f"invalid daemon frame: {error}")
        finally:
            self._fail_all(failure)

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
