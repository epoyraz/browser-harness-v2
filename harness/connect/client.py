"""Client side of the daemon: talk CDP over IPC (DESIGN.md D5/D7).

`RemoteConnection` implements the same three things `Tab` asks of a `Connection` —
`request()`, `subscribe()`, `journal` — so every primitive in `ops/` works unchanged
whether it is driving Chrome directly or driving it through the daemon. That symmetry is
the whole reason `Connection` was kept a dumb multiplexer: the seam was already in the
right place.

Why go through the daemon at all, when a client could open its own websocket? Because a
second websocket to an already-authorised Chrome is denied a consent prompt every time
(D7, measured 0/6). One held connection, N clients.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, Self

from harness.connect.session import STATE_CLASS, Session, State
from harness.core import ipc
from harness.core.journal import Journal
from harness.core.outcome import (
    BrowserDisconnected,
    Class,
    HarnessError,
    Timeout,
    fail,
)

#: How long to wait for a freshly spawned daemon to answer `ping`.
SPAWN_TIMEOUT = 30.0


def ensure_daemon(name: str = "default", *, timeout: float = SPAWN_TIMEOUT) -> dict[str, Any]:
    """Return the daemon's pong, spawning it if nothing is listening.

    The spawned process is detached and outlives this client, because its whole purpose is
    to hold one browser connection across many short-lived client runs.
    """
    if (pong := ipc.ping(name)) is not None:
        return pong
    subprocess.Popen(
        [sys.executable, "-m", "harness.cli.main", "daemon", name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, start_new_session=True,
        env={**os.environ, "PYTHONPATH": os.environ.get("PYTHONPATH", "")},
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (pong := ipc.ping(name)) is not None:
            return pong
        time.sleep(0.1)
    raise BrowserDisconnected(
        f"daemon {name!r} did not come up in {timeout}s — run `bh --doctor` to see which "
        f"endpoint strategies declined", daemon=name)


class RemoteConnection:
    """A `Connection` that lives in another process. Same interface, one socket.

    Requests and events share the socket, so a reader thread demultiplexes exactly as the
    direct `Connection` does: frames with `id` complete a pending call, frames carrying
    `event` fan out to subscribers.
    """

    def __init__(self, name: str = "default", *, journal: Journal | None = None,
                 timeout: float = 30.0):
        self.name = name
        self._j = journal or Journal(None)
        self._sock, self._token = ipc.connect(name, timeout=timeout)
        self._sock.settimeout(None)
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, dict[str, Any]] = {}
        self._events: list[Callable[[dict[str, Any]], None]] = []
        self._closed = False
        self._buf = bytearray()
        self._reader = threading.Thread(target=self._pump, name="bh-client", daemon=True)
        self._reader.start()
        self._call({"meta": "subscribe"}, timeout=10.0)

    # -- the Connection interface -----------------------------------------

    @property
    def journal(self) -> Journal:
        return self._j

    def subscribe(self, fn: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            self._events.append(fn)

    def unsubscribe(self, fn: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            if fn in self._events:
                self._events.remove(fn)

    def request(self, method: str, params: dict[str, Any] | None = None, *,
                session_id: str | None = None, timeout: float = 20.0) -> dict[str, Any]:
        payload: dict[str, Any] = {"method": method, "params": params or {},
                                   "timeout": timeout}
        if session_id:
            payload["session_id"] = session_id
        self._j.cdp(method)
        reply = self._call(payload, timeout=timeout + 5.0)
        if reply.get("ok"):
            return reply.get("value") or {}
        raise HarnessError.of(fail(Class(reply.get("class", Class.CDP_ERROR.value)),
                                   reply.get("detail", ""),
                                   **(reply.get("observed") or {})))

    def close(self) -> None:
        with self._lock:
            self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False

    # -- plumbing ----------------------------------------------------------

    def _call(self, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise BrowserDisconnected("client connection is closed")
            self._next_id += 1
            rid = self._next_id
            slot: dict[str, Any] = {"done": threading.Event()}
            self._pending[rid] = slot
        body = {**payload, "rid": rid}
        if self._token:
            body["token"] = self._token
        try:
            self._sock.sendall((json.dumps(body, default=str) + "\n").encode())
        except OSError as e:
            with self._lock:
                self._pending.pop(rid, None)
            raise BrowserDisconnected(f"daemon went away: {e}") from e
        if not slot["done"].wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise Timeout(f"daemon did not answer {payload.get('method') or payload.get('meta')}"
                          f" in {timeout}s", daemon=self.name)
        if "error" in slot:
            raise slot["error"]
        return slot["reply"]

    def _pump(self) -> None:
        while True:
            with self._lock:
                if self._closed:
                    return
            try:
                chunk = self._sock.recv(1 << 16)
            except OSError:
                self._fail_all(BrowserDisconnected("daemon connection lost"))
                return
            if not chunk:
                self._fail_all(BrowserDisconnected("daemon closed the connection"))
                return
            self._buf += chunk
            while b"\n" in self._buf:
                line, _, rest = bytes(self._buf).partition(b"\n")
                self._buf = bytearray(rest)
                if line.strip():
                    self._dispatch(line)

    def _dispatch(self, line: bytes) -> None:
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            return
        if "event" in frame:
            with self._lock:
                handlers = list(self._events)
            for fn in handlers:
                try:
                    fn(frame["event"])
                except Exception as e:  # noqa: BLE001 — one bad handler must not deafen the rest
                    self._j.write("note", msg=f"client event handler failed: {e}")
            return
        rid = frame.pop("rid", None)
        with self._lock:
            slot = self._pending.pop(rid, None)
        if slot is None:
            return                          # a call that already timed out
        slot["reply"] = frame
        slot["done"].set()

    def _fail_all(self, error: BaseException) -> None:
        with self._lock:
            slots = list(self._pending.values())
            self._pending.clear()
        for slot in slots:
            slot["error"] = error
            slot["done"].set()


class RemoteRegistry:
    """Client-side view of the daemon's session registry.

    Caches `targetId → sessionId` so a hot loop does not pay an IPC round trip per call,
    and drops the cache when the daemon reports the session dead — the registry over there
    is still the single producer (D1); this only remembers what it said.
    """

    def __init__(self, conn: RemoteConnection):
        self._conn = conn
        self._cache: dict[str, Session] = {}
        self._lock = threading.Lock()
        conn.subscribe(self._on_event)

    def ready_session(self, target_id: str) -> Session:
        with self._lock:
            hit = self._cache.get(target_id)
        if hit is not None:
            return hit
        got = self._conn._call({"meta": "attach", "target_id": target_id}, timeout=30.0)
        if not got.get("ok"):
            raise HarnessError.of(fail(Class(got.get("class", Class.TARGET_GONE.value)),
                                       got.get("detail", ""),
                                       **(got.get("observed") or {})))
        value = got["value"]
        session = Session(target_id=target_id, session_id=value["session_id"])
        with self._lock:
            self._cache[target_id] = session
        return session

    def ensure_live(self, target_id: str) -> Session:
        return self.ready_session(target_id)

    def forget(self, target_id: str) -> None:
        with self._lock:
            self._cache.pop(target_id, None)

    def _on_event(self, msg: dict[str, Any]) -> None:
        """A target that died invalidates its cached session, so the next call re-attaches
        rather than issuing a command against a session the browser has already dropped."""
        method = msg.get("method")
        params = msg.get("params") or {}
        if method in ("Target.targetDestroyed", "Target.targetCrashed"):
            self.forget(params.get("targetId", ""))
        elif method == "Target.detachedFromTarget":
            sid = params.get("sessionId")
            with self._lock:
                dead = [t for t, s in self._cache.items() if s.session_id == sid]
                for t in dead:
                    self._cache.pop(t, None)


__all__ = ["STATE_CLASS", "RemoteConnection", "RemoteRegistry", "State", "ensure_daemon"]
