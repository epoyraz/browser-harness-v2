"""The daemon: one browser connection, many clients (DESIGN.md D5/D7, TODO 7).

The process model, settled by measurement rather than taste (D7): a second websocket to an
already-authorised Chrome is refused a fresh consent prompt every time, and Chrome
serialises the prompts it does show. Per-client daemons would therefore put a modal in
front of every subagent. **One daemon per browser endpoint, multiplexing N clients.**

A client request names its target. The daemon resolves it through `ready_session()` — the
one producer — and issues the command on that session. Nothing here keeps a "current tab":
that shared mutable cursor is what made two v1 subagents fight over one tab (#375). The
target is a parameter, so two clients driving two tabs never interact.

Every reply is an outcome (D11). The daemon never returns a bare string for a failure,
because that is what forced v1's clients to string-match Chrome's prose back out.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Self

from harness.connect.cdp import Connection
from harness.connect.session import SessionRegistry
from harness.core import ipc
from harness.core.journal import Journal
from harness.core.outcome import BrowserDisconnected, Class, HarnessError, fail, ok

#: In-flight requests one client may have. Past this they queue — the same backpressure
#: the old serial loop always had, but only once genuinely saturated.
_CLIENT_WORKERS = int(os.environ.get("BH_DAEMON_WORKERS") or 16)

#: Raw-CDP methods that would otherwise produce a session behind the registry's back.
_SESSION_METHODS = frozenset({"Target.attachToTarget", "Target.detachFromTarget"})


class _Peer:
    """One client socket, with the lock that keeps replies and events from interleaving.

    Two threads write here — the client's own handler thread (replies) and the CDP reader
    thread (events) — so an unguarded `sendall` would splice two JSON lines together.
    """

    __slots__ = ("lock", "sock")

    def __init__(self, sock: socket.socket):
        self.sock, self.lock = sock, threading.Lock()

    def send(self, payload: dict[str, Any]) -> bool:
        line = (json.dumps(payload, default=str) + "\n").encode()
        try:
            with self.lock:
                self.sock.sendall(line)
            return True
        except OSError:
            return False

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Daemon:
    """Serves the IPC socket. Owns exactly one `Connection` and one `SessionRegistry`."""

    def __init__(self, name: str, transport: Any, *, journal: Journal | None = None,
                 token: str | None = None):
        self.name = ipc.check_name(name)
        self.journal = journal or Journal(None)
        # A callable defers the handshake until after the endpoint is published; a live
        # transport is used as-is, which is what every unit test passes.
        self._make_transport = transport if callable(transport) else None
        self.conn = Connection(None if self._make_transport else transport,
                               journal=self.journal)
        self.sessions = SessionRegistry(self.conn, journal=self.journal)
        self._token = token
        self._server: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._peers: set[_Peer] = set()
        self._plock = threading.Lock()
        #: Set once the browser handshake has *finished*, successfully or not. The IPC
        #: endpoint is published before this, so a client can always reach the daemon and
        #: be told what is pending instead of waiting out a silent timeout.
        self._settled = threading.Event()
        self._connect_error = ""
        self._connect_started = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> Self:
        """Publish the endpoint first, open the browser connection second.

        The handshake is unbounded work: Chrome M144 shows an "Allow remote debugging"
        prompt **per websocket** and blocks until someone answers it. Connecting before
        binding meant the port file appeared only after that click, so a waiting client
        saw 30 s of silence — indistinguishable from a daemon that never started, and with
        the spawned daemon's stderr going to DEVNULL there was nothing to read either.
        Binding first makes the daemon pingable throughout, so it can say what it is
        waiting for.
        """
        self._server = ipc.bind(self.name)
        # On Windows `bind()` mints the token it published in the port file; adopting it is
        # what makes `_answer`'s check a real boundary rather than a no-op. None on POSIX.
        if self._token is None:
            self._token = ipc.expected_token()
        self._server.settimeout(0.5)
        self._connect_started = True
        threading.Thread(target=self._open_browser, daemon=True,
                         name=f"bh-connect-{self.name}").start()
        return self

    def _open_browser(self) -> None:
        """The browser half of startup. Failures are recorded, never raised: this runs on
        its own thread, and a client asking `ping` deserves the reason, not a dead socket."""
        try:
            if self._make_transport is not None:
                # THIS is the call Chrome blocks: the websocket handshake waits on the
                # consent prompt. It happens here, after bind(), so the daemon is already
                # answering pings and can report that it is waiting.
                self.conn.attach(self._make_transport())
            self.conn.start()
            self.conn.subscribe(self._watch_disconnect)
            self.conn.subscribe(self._broadcast)
            self.sessions.discover()
        except Exception as e:                       # noqa: BLE001 — reported, not raised
            self._connect_error = f"{type(e).__name__}: {str(e)[:200]}"
            self.journal.write("daemon", event="connect_failed", error=self._connect_error)
        finally:
            self._settled.set()

    def _browser_pending(self, timeout: float) -> dict[str, Any] | None:
        """`None` when the browser is usable, else the typed outcome explaining why not."""
        if not self._connect_started:
            return None                  # conn is being driven directly (unit tests)
        if not self._settled.wait(timeout=max(0.0, min(timeout, 30.0))):
            return fail(Class.BROWSER_DISCONNECTED,
                        "browser connection has not opened yet — Chrome shows an 'Allow "
                        "remote debugging' prompt per websocket and blocks until it is "
                        "answered", daemon=self.name, connecting=True).to_json()
        if self._connect_error:
            return fail(Class.BROWSER_DISCONNECTED,
                        f"browser connection failed: {self._connect_error}",
                        daemon=self.name).to_json()
        return None

    def serve_forever(self) -> None:
        assert self._server is not None, "call start() first"
        while not self._stop.is_set():
            try:
                client, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            thread = threading.Thread(target=self._serve_client, args=(client,), daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        # The connect thread may still be mid-handshake; closing an unopened transport
        # must not turn teardown into an exception.
        with contextlib.suppress(Exception):
            self.conn.close()
        ipc.cleanup(self.name)

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc: object) -> bool:
        self.stop()
        return False

    # -- serving -----------------------------------------------------------

    def _serve_client(self, client: socket.socket) -> None:
        """One thread per client connection, and each request dispatched off that thread.

        Answering inline in the read loop was the original shape, and it made one client's
        requests strictly serial: the loop could not read request N+1 until the CDP round
        trip for N had returned. Two *clients* still overlapped, which is what the previous
        comment claimed and measured — but `parallel()` is one client with N worker
        threads, and it saw no speedup at all until this changed (12 pages across 6 workers
        took as long as 12 pages one at a time).

        Dispatching is safe because nothing here assumes reply order: every reply carries
        the client's `rid`, `_Peer.send` is serialised by its own lock so replies and
        pushed events cannot splice, and the CDP connection below already multiplexes.
        """
        peer = _Peer(client)
        buf = bytearray()
        # Bounded: an unbounded thread-per-request would let a client with a runaway loop
        # exhaust the daemon. Requests past the cap queue, which is the same backpressure
        # the serial loop had, but only once genuinely saturated.
        pool = ThreadPoolExecutor(max_workers=_CLIENT_WORKERS,
                                  thread_name_prefix="bh-daemon-req")

        def answer_and_send(line: bytes) -> None:
            try:
                reply = self._answer(line, peer)
                # Echo the client's request id so a multiplexed client can match the
                # reply to the call, the same way CDP's own `id` works.
                if (rid := _rid_of(line)) is not None:
                    reply = {**reply, "rid": rid}
                peer.send(reply)
            except OSError:
                pass        # the peer went away mid-reply; the read loop will notice

        try:
            while not self._stop.is_set():
                chunk = client.recv(1 << 16)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, _, rest = bytes(buf).partition(b"\n")
                    buf = bytearray(rest)
                    pool.submit(answer_and_send, line)
        except OSError:
            return          # a client that vanishes mid-request is normal, not an error
        finally:
            # Do not wait: a client that disconnected is not owed its in-flight replies,
            # and joining here would hold the connection thread open for a full timeout.
            pool.shutdown(wait=False, cancel_futures=True)
            with self._plock:
                self._peers.discard(peer)
            peer.close()

    def _answer(self, line: bytes, peer: _Peer | None = None) -> dict[str, Any]:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            return fail(Class.CDP_ERROR, "malformed request").to_json()
        if not isinstance(request, dict):
            return fail(Class.CDP_ERROR, "request must be an object").to_json()
        if self._token and request.get("token") != self._token:
            # Loopback has no chmod equivalent, so the token is the only boundary on Windows.
            return fail(Class.SCOPE_REFUSED, "bad or missing token").to_json()
        if request.get("meta") == "subscribe" and peer is not None:
            # A client that wants CDP events gets them pushed on this same socket. The
            # alternative — a second connection — would cost a consent prompt (D7), and
            # polling would reintroduce exactly the latency D13 removed.
            with self._plock:
                self._peers.add(peer)
            return _value(ok({"subscribed": True}))
        return self.handle(request)

    def _broadcast(self, msg: dict[str, Any]) -> None:
        """Fan a CDP event out to subscribed clients. Runs on the CDP reader thread, so it
        must never block: a peer that has gone away is dropped, not waited on."""
        with self._plock:
            peers = list(self._peers)
        if not peers:
            return
        frame = {"event": msg}
        for peer in peers:
            if not peer.send(frame):
                with self._plock:
                    self._peers.discard(peer)

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        """Answer one request. Always an outcome, never a bare value or a bare string."""
        meta = request.get("meta")
        if meta:
            return self._meta(meta, request)
        method = request.get("method")
        if not method:
            return fail(Class.CDP_ERROR, "request names no method").to_json()
        if (pending := self._browser_pending(float(request.get("timeout", 20.0)))) is not None:
            return pending
        if method in _SESSION_METHODS:
            # The raw-CDP surface is an escape hatch, not a second way to make a session.
            # v1's `helpers.js()` called `Target.attachToTarget` directly on every call and
            # leaked an un-domained session each time; routing the method through the one
            # producer closes that by construction rather than by convention.
            return self._session_method(method, request)
        try:
            target_id = request.get("target_id")
            session_id = request.get("session_id")
            if target_id and not session_id:
                session_id = self.sessions.ensure_live(target_id).session_id
            result = self.conn.request(
                method, request.get("params") or {},
                session_id=session_id,
                timeout=float(request.get("timeout", 20.0)),
            )
            return _value(ok(result))
        except HarnessError as e:
            # rule 2: the cause reaches the caller intact, typed, with its evidence
            return e.outcome.to_json()

    def _session_method(self, method: str, request: dict[str, Any]) -> dict[str, Any]:
        """Answer `Target.attachToTarget` / `detachFromTarget` from the registry.

        The reply keeps CDP's own shape, so a caller reaching for raw CDP gets what it
        expects — it simply gets a registered, domain-enabled, idempotent session instead
        of a fresh anonymous one.
        """
        params = request.get("params") or {}
        try:
            if method == "Target.detachFromTarget":
                target_id = params.get("targetId") or request.get("target_id")
                if not target_id:
                    return fail(Class.CDP_ERROR, "detachFromTarget needs a targetId").to_json()
                self.sessions.forget(target_id)
                return _value(ok({}))
            target_id = params.get("targetId") or request.get("target_id")
            if not target_id:
                return fail(Class.CDP_ERROR, "attachToTarget needs a targetId").to_json()
            session = self.sessions.ready_session(target_id)
            return _value(ok({"sessionId": session.session_id}))
        except HarnessError as e:
            return e.outcome.to_json()

    def _meta(self, meta: str, request: dict[str, Any]) -> dict[str, Any]:
        if meta == "ping":
            # Liveness means *both* processes are alive: a meta-only pong from a daemon whose
            # browser socket is dead is what v1 needed six PRs to stop reporting as healthy.
            # So `browser` stays the readiness signal — but the pong now also carries WHY it
            # is false, which is the difference between "still waiting on your click" and
            # "never coming up".
            settled = self._settled.is_set() or not self._connect_started
            live = settled and not self._connect_error and not self.conn._closed
            out: dict[str, Any] = {"pong": True, "browser": live,
                                   "targets": self.sessions.live_targets}
            if not live:
                out["connecting"] = not settled
                out["reason"] = self._connect_error or (
                    "browser handshake still open — Chrome prompts per websocket and "
                    "blocks until it is answered" if not settled
                    else "browser connection is closed")
            return out
        if (pending := self._browser_pending(20.0)) is not None and meta in ("attach", "forget"):
            return pending
        if meta == "attach":
            try:
                return _value(ok(self.sessions.ready_session(request["target_id"]).to_json()))
            except HarnessError as e:
                return e.outcome.to_json()
        if meta == "forget":
            self.sessions.forget(request["target_id"])
            return _value(ok(None))
        return fail(Class.CDP_ERROR, f"unknown meta {meta!r}").to_json()

    # -- events ------------------------------------------------------------

    def _watch_disconnect(self, msg: dict[str, Any]) -> None:
        if msg.get("method") == "Inspector.detached":
            self.sessions.disconnected(str((msg.get("params") or {}).get("reason", "")))


def _rid_of(line: bytes) -> Any:
    try:
        return json.loads(line).get("rid")
    except (json.JSONDecodeError, AttributeError):
        return None


def _value(outcome: Any) -> dict[str, Any]:
    """Outcome JSON plus its value. `to_json()` deliberately omits the payload so a journal
    line stays small; a wire reply is the one place it must be carried."""
    return {**outcome.to_json(), "value": outcome.value}


def serve(name: str = "default", *, journal_path: str | None = None) -> int:
    """Discovery → transport → daemon → serve. The missing wire: until this existed, the
    daemon had never been connected to a real browser, only to a fake in unit tests."""
    from harness.connect.cdp import WebSocketTransport
    from harness.connect.endpoint import binding_for, resolve

    resolution = resolve(binding_for(name))
    journal = Journal(journal_path, session=name) if journal_path else Journal(None)
    journal.write("daemon", event="serving", ws=resolution.ws_url,
                  strategy=resolution.strategy)
    # A factory, not a live transport: constructing WebSocketTransport performs the
    # handshake, and doing that before Daemon.start() meant the port file appeared only
    # after Chrome's consent prompt was answered.
    daemon = Daemon(name, lambda: WebSocketTransport(resolution.ws_url),
                    journal=journal).start()
    try:
        daemon.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        daemon.stop()
    return 0


def request(name: str, payload: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    """Client side of one request. Raises the typed error, so callers branch on a class."""
    sock, token = ipc.connect(name, timeout=timeout)
    try:
        reply = ipc.request(sock, token, payload)
    finally:
        try:
            sock.close()
        except OSError:
            pass
    if reply.get("pong"):
        return reply
    if reply.get("ok"):
        return reply
    cls = Class(reply.get("class", Class.CDP_ERROR.value))
    raise HarnessError.of(fail(cls, reply.get("detail", ""), **(reply.get("observed") or {})))


__all__ = ["BrowserDisconnected", "Daemon", "request"]
