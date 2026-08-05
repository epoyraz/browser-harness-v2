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

import json
import socket
import threading
from typing import Any, Self

from harness.connect.cdp import Connection
from harness.connect.session import SessionRegistry
from harness.core import ipc
from harness.core.journal import Journal
from harness.core.outcome import BrowserDisconnected, Class, HarnessError, fail, ok

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
        self.conn = Connection(transport, journal=self.journal)
        self.sessions = SessionRegistry(self.conn, journal=self.journal)
        self._token = token
        self._server: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._peers: set[_Peer] = set()
        self._plock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> Self:
        self.conn.start()
        self.conn.subscribe(self._watch_disconnect)
        self.conn.subscribe(self._broadcast)
        self.sessions.discover()
        self._server = ipc.bind(self.name)
        self._server.settimeout(0.5)
        return self

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
        self.conn.close()
        ipc.cleanup(self.name)

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc: object) -> bool:
        self.stop()
        return False

    # -- serving -----------------------------------------------------------

    def _serve_client(self, client: socket.socket) -> None:
        """One thread per client connection, each request answered independently.

        Threads are what make the done-when true: two clients issuing long CDP calls against
        two tabs overlap in flight, rather than one waiting out the other.
        """
        peer = _Peer(client)
        buf = bytearray()
        try:
            while not self._stop.is_set():
                chunk = client.recv(1 << 16)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, _, rest = bytes(buf).partition(b"\n")
                    buf = bytearray(rest)
                    reply = self._answer(line, peer)
                    # Echo the client's request id so a multiplexed client can match the
                    # reply to the call, the same way CDP's own `id` works.
                    if (rid := _rid_of(line)) is not None:
                        reply = {**reply, "rid": rid}
                    peer.send(reply)
        except OSError:
            return          # a client that vanishes mid-request is normal, not an error
        finally:
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
            return {"pong": True, "browser": not self.conn._closed,
                    "targets": self.sessions.live_targets}
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
    daemon = Daemon(name, WebSocketTransport(resolution.ws_url), journal=journal).start()
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
