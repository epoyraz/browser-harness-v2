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
    ProtocolMismatch,
    Timeout,
    fail,
)
from harness.version import PROTOCOL_VERSION, VERSION

#: How long to wait for a freshly spawned daemon to answer `ping`.
SPAWN_TIMEOUT = 30.0


def _validate_protocol(reply: dict[str, Any]) -> None:
    got = reply.get("protocol")
    if got != PROTOCOL_VERSION:
        raise ProtocolMismatch(
            f"client protocol {PROTOCOL_VERSION} cannot use daemon protocol {got!r}",
            client_protocol=PROTOCOL_VERSION, daemon_protocol=got,
            client_version=VERSION, daemon_version=reply.get("version"))


def _spawn_kwargs() -> dict[str, Any]:
    """Detach the daemon from this terminal so it outlives the client.

    `start_new_session` is POSIX-only and silently ignored on Windows, which needs creation
    flags instead. DETACHED_PROCESS is deliberately omitted: per the Win32 docs it overrides
    CREATE_NO_WINDOW and Windows then allocates a fresh console for the daemon.
    """
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def _spawn_daemon(name: str) -> None:
    """Start a detached daemon. It outlives this client by design: its whole purpose is to
    hold one browser connection across many short-lived client runs."""
    log = ipc.ensure_private(ipc.runtime_dir()) / f"{ipc.check_name(name)}.log"
    # Not DEVNULL: a daemon that died during startup used to leave nothing behind at all,
    # which is exactly why its failures could not be diagnosed.
    with open(log, "ab", buffering=0) as fh:
        subprocess.Popen(
            [sys.executable, "-m", "harness.cli.main", "daemon", name],
            stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
            env={**os.environ, "PYTHONPATH": os.environ.get("PYTHONPATH", "")},
            **_spawn_kwargs(),
        )


def ensure_daemon(name: str = "default", *, timeout: float = SPAWN_TIMEOUT) -> dict[str, Any]:
    """Return the daemon's pong once its **browser** connection is live, spawning it first
    if nothing is listening.

    A pong alone is not readiness. The daemon publishes its IPC endpoint before opening the
    browser handshake, so it answers while Chrome is still showing its per-websocket consent
    prompt. Waiting on `browser` keeps the v1 rule that alive means *both* processes, while
    the pong's `reason` turns the pending case from silence into a sentence.

    The spawned process is detached and outlives this client, because its whole purpose is
    to hold one browser connection across many short-lived client runs.
    """
    if (pong := ipc.ping(name)) is not None and pong.get("browser"):
        _validate_protocol(pong)
        return pong
    spawns = 0
    spawned_since_gone = False
    if pong is None:
        _spawn_daemon(name)
        spawns = 1
        spawned_since_gone = True
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = pong or {}
    while time.monotonic() < deadline:
        if (pong := ipc.ping(name)) is not None:
            if pong.get("browser"):
                _validate_protocol(pong)
                return pong
            last = pong
            spawned_since_gone = False     # it is answering again; a later loss is new news
        elif spawns < 2 and not spawned_since_gone:
            # A daemon we HAD just seen is now gone. That is not an error: when its browser
            # dies the daemon exits on purpose, unlinking its socket so a clean one can take
            # over. Without a spawn here the client that arrived during that window never
            # spawned anything and waited out the whole deadline for a process that was
            # deliberately leaving — 30s of hanging and then a hard failure on the first
            # `bh` after Chrome restarts, with only a second invocation recovering.
            # Bounded at one extra spawn so a daemon that dies on startup cannot loop.
            _spawn_daemon(name)
            spawns += 1
            spawned_since_gone = True      # one replacement per disappearance, not per tick
            last = {}                      # the old daemon's reason no longer describes us
        time.sleep(0.1)
    if last:
        # The daemon is up and talking; it simply has no browser yet — and it told us why.
        raise BrowserDisconnected(
            f"daemon {name!r} is running but its browser connection did not open in "
            f"{timeout}s: {last.get('reason') or 'no reason reported'}",
            daemon=name, connecting=bool(last.get("connecting")),
            reason=last.get("reason", ""))
    raise BrowserDisconnected(
        f"daemon {name!r} did not come up in {timeout}s — see "
        f"{ipc.runtime_dir() / (name + '.log')}, or run `bh --doctor` to see which "
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
        # AF_UNIX and TCP are byte streams: concurrent sendall() calls aren't message
        # boundaries and may interleave when either call needs more than one write. Reads
        # have a dedicated pump; writes need the same single-owner rule so newline-delimited
        # JSON remains one request per line under parallel().
        self._send_lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, dict[str, Any]] = {}
        self._events: list[Callable[[dict[str, Any]], None]] = []
        self._closed = False
        #: The diagnosed cause of the reader's death, handed to the next caller. D11 rule 2
        #: — never discard a cause you were handed.
        self._failure: BaseException | None = None
        self._buf = bytearray()
        self._reader = threading.Thread(target=self._pump, name="bh-client", daemon=True)
        self._reader.start()
        subscribed = self._call({"meta": "subscribe"}, timeout=10.0)
        value = subscribed.get("value") or {}
        _validate_protocol(value)

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
        marker = self._j.cdp_start(method, params)
        try:
            reply = self._call(payload, timeout=timeout + 5.0)
        except BaseException as error:
            self._j.cdp_end(marker, method, error=error)
            raise
        if reply.get("ok"):
            result = reply.get("value") or {}
            self._j.cdp_end(marker, method, result=result)
            return result
        error = HarnessError.of(fail(Class(reply.get("class", Class.CDP_ERROR.value)),
                                     reply.get("detail", ""),
                                     **(reply.get("observed") or {})))
        self._j.cdp_end(marker, method, error=error)
        raise error

    def close(self) -> None:
        with self._lock:
            self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass

    def adopt_default_target(self, *, exclude: list[str] | None = None) -> dict[str, Any]:
        """Ask the daemon for this client's default tab.

        The daemon picks the first drivable page no other connected client has adopted,
        or creates a fresh background tab when every page is spoken for — atomically, so
        two clients starting together cannot land on the same page. Raises on a daemon
        that predates the meta; `Session.tab()` falls back to the legacy client-side scan
        there, because a stale long-lived daemon must degrade to the old behaviour rather
        than refuse to hand out a tab at all.
        """
        payload: dict[str, Any] = {"meta": "adopt"}
        if exclude:
            payload["exclude"] = list(exclude)
        got = self._call(payload, timeout=30.0)
        if not got.get("ok"):
            raise HarnessError.of(fail(Class(got.get("class", Class.CDP_ERROR.value)),
                                       got.get("detail", ""),
                                       **(got.get("observed") or {})))
        return dict(got["value"] or {})

    def create_target_lease(self, target_id: str) -> str:
        """Mint an opaque, daemon-owned capability for one explicit target."""
        got = self._call({"meta": "lease_create", "target_id": target_id}, timeout=30.0)
        if not got.get("ok"):
            raise HarnessError.of(fail(Class(got.get("class", Class.CDP_ERROR.value)),
                                       got.get("detail", ""),
                                       **(got.get("observed") or {})))
        return str(got["value"]["lease"])

    def claim_target_lease(self, lease: str) -> str:
        """Resolve a lease to its target, failing closed when it is stale or unknown."""
        got = self._call({"meta": "lease_claim", "lease": lease}, timeout=30.0)
        if not got.get("ok"):
            raise HarnessError.of(fail(Class(got.get("class", Class.CDP_ERROR.value)),
                                       got.get("detail", ""),
                                       **(got.get("observed") or {})))
        return str(got["value"]["target_id"])

    def release_target_lease(self, lease: str) -> None:
        got = self._call({"meta": "lease_release", "lease": lease}, timeout=30.0)
        if not got.get("ok"):
            raise HarnessError.of(fail(Class(got.get("class", Class.CDP_ERROR.value)),
                                       got.get("detail", ""),
                                       **(got.get("observed") or {})))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False

    # -- plumbing ----------------------------------------------------------

    def _call(self, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                # The reader diagnosed why it died; report THAT, not a generic disconnect.
                # The bare fallback is for a connection we closed ourselves, where there is
                # no cause to pass on.
                raise self._failure or BrowserDisconnected("client connection is closed")
            self._next_id += 1
            rid = self._next_id
            slot: dict[str, Any] = {"done": threading.Event()}
            self._pending[rid] = slot
        body = {**payload, "rid": rid}
        if self._token:
            body["token"] = self._token
        try:
            with self._send_lock:
                self._sock.sendall((json.dumps(body, default=str) + "\n").encode())
        except OSError as e:
            with self._lock:
                self._pending.pop(rid, None)
                failure = self._failure
            if failure is not None:
                # The reader can diagnose a malformed reply and close the transport while
                # this thread is still inside sendall(). Its ProtocolMismatch is the cause;
                # the resulting write error is only fallout from that terminal decision.
                raise failure
            raise BrowserDisconnected(f"daemon went away: {e}") from e
        if not slot["done"].wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise Timeout(f"daemon did not answer {payload.get('method') or payload.get('meta')}"
                          f" in {timeout}s", daemon=self.name)
        if "error" in slot:
            raise slot["error"]
        return slot["reply"]

    def _die(self, failure: BaseException) -> None:
        """Mark the connection dead, keeping the cause.

        The reader is the only thing that can resolve a pending request, so its death IS
        the connection's death. Leaving `_closed` False let the next `_call` write into a
        dead socket and wait out the full timeout, then report `Timeout` for a connection
        we had already diagnosed — the wrong class, and the real cause discarded. Only
        claim it if `close()` has not already done so, so a deliberate close keeps its own
        account of itself. Mirrors AsyncConnection._pump in harness/aio.py.
        """
        claimed = False
        with self._lock:
            if not self._closed:
                self._closed = True
                self._failure = failure
                claimed = True
        self._fail_all(failure)
        if claimed:
            try:
                self._sock.close()
            except OSError:
                pass

    def _pump(self) -> None:
        # The blanket handler is not defensive padding: ANY escape from this loop ends the
        # only thread that can resolve a pending request, so every exit has to go through
        # `_die`. Without it an unexpected error in `_dispatch` would kill the reader
        # silently and leave the connection reporting itself open forever.
        try:
            while True:
                with self._lock:
                    if self._closed:
                        return
                try:
                    chunk = self._sock.recv(1 << 16)
                except OSError as e:
                    self._die(BrowserDisconnected(f"daemon connection lost: {e}"))
                    return
                if not chunk:
                    self._die(BrowserDisconnected("daemon closed the connection"))
                    return
                self._buf += chunk
                while b"\n" in self._buf:
                    line, _, rest = bytes(self._buf).partition(b"\n")
                    self._buf = bytearray(rest)
                    if line.strip():
                        self._dispatch(line)
        except ProtocolMismatch as error:
            # Preserve a framing diagnosis exactly as computed by `_dispatch`, including
            # its JSON decoder cause. Wrapping it in another ProtocolMismatch here would
            # make the original parse failure disappear from the exception chain.
            self._j.write("note", msg=str(error))
            self._die(error)
        except Exception as e:          # noqa: BLE001 — recorded, never raised
            # Not re-raised: this is a daemon thread, so raising only prints to stderr and
            # tells no one who could act. The cause is stored on the connection instead,
            # where the next `_call` reports it as a typed outcome, and noted in the journal
            # for the post-mortem.
            failure = ProtocolMismatch(f"client reader failed: {type(e).__name__}: {e}")
            self._j.write("note", msg=str(failure))
            self._die(failure)

    def _dispatch(self, line: bytes) -> None:
        try:
            frame = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ProtocolMismatch(f"invalid daemon JSON frame: {error}") from error
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

    def prepare_runtime(self, target_id: str) -> Session:
        """Ask the persistent daemon to prepare this session generation once."""
        got = self._conn._call(
            {"meta": "prepare_runtime", "target_id": target_id}, timeout=30.0
        )
        if not got.get("ok"):
            raise HarnessError.of(
                fail(
                    Class(got.get("class", Class.TARGET_GONE.value)),
                    got.get("detail", ""),
                    **(got.get("observed") or {}),
                )
            )
        value = got["value"]
        session = Session(target_id=target_id, session_id=value["session_id"])
        with self._lock:
            self._cache[target_id] = session
        return session

    def ensure_domains(self, target_id: str, domains: tuple[str, ...]) -> Session:
        got = self._conn._call(
            {"meta": "ensure_domains", "target_id": target_id, "domains": list(domains)},
            timeout=30.0,
        )
        if not got.get("ok"):
            raise HarnessError.of(
                fail(
                    Class(got.get("class", Class.TARGET_GONE.value)),
                    got.get("detail", ""),
                    **(got.get("observed") or {}),
                )
            )
        value = got["value"]
        session = Session(target_id=target_id, session_id=value["session_id"])
        with self._lock:
            self._cache[target_id] = session
        return session

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
