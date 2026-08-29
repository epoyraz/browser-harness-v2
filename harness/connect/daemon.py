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
import queue
import secrets
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Self

from harness.connect.cdp import MAX_FRAME, Connection
from harness.connect.endpoint import BrowserIdentity, safe_endpoint
from harness.connect.session import SessionRegistry
from harness.core import ipc
from harness.core.journal import Journal
from harness.core.outcome import BrowserDisconnected, Class, HarnessError, fail, ok
from harness.version import PROTOCOL_VERSION, VERSION

#: A tab the adopt fallback may hand out. `about:blank` counts; chrome:// internals and
#: devtools do not. The single definition: `Session` used to carry a second copy for its
#: own client-side scan, and two hand-synchronised answers to "may I drive this?" is how
#: the two sides come to disagree.
_DRIVABLE = ("http://", "https://", "file://", "about:blank", "data:")

#: In-flight requests one client may have. Past this they queue — the same backpressure
#: the old serial loop always had, but only once genuinely saturated.
_DAEMON_WORKERS = int(os.environ.get("BH_DAEMON_WORKERS") or 16)
_DAEMON_QUEUE = max(_DAEMON_WORKERS, int(os.environ.get("BH_DAEMON_QUEUE") or 64))

#: How long a daemon whose browser connection is settled-and-dead keeps answering before it
#: exits. Not a retry budget — the daemon never re-handshakes (see `_browser_is_dead`). It is
#: the window in which a client already blocked in `ensure_daemon` can still read the typed
#: `reason` off a pong; after it, the socket goes away so the NAME is free to be respawned.
DEATH_LINGER = 5.0

#: Raw-CDP methods that would otherwise produce a session behind the registry's back.
_SESSION_METHODS = frozenset({"Target.attachToTarget", "Target.detachFromTarget"})

#: A daemon frame contains one decoded CDP frame plus a small outcome/event envelope.
#: `_encode_frame` uses compact UTF-8 JSON so decoding and re-encoding cannot multiply
#: non-ASCII payloads. One MiB is consequently ample room for the fixed envelope while
#: keeping the local protocol close to the browser transport's deliberate 100 MiB cap.
MAX_DAEMON_FRAME = MAX_FRAME + (1 << 20)

#: Per-client output is owned by one writer thread.  Count and bytes are both bounded:
#: bounding only the frame count would still permit dozens of 100 MiB CDP frames to sit
#: behind a peer that stopped reading.
# A complex navigation can emit well over 64 small Page/Runtime/Network events before the
# Windows socket writer gets its next time slice. 64 therefore treated a healthy reader as
# stalled and severed its IPC connection in the middle of goto(); the next `bh` invocation
# could immediately read the loaded page, proving that neither Chrome nor the daemon had
# died. Keep the byte ceiling as the real memory bound, but give ordinary event bursts room
# to drain. A genuinely stalled peer is still evicted by that byte ceiling (or this count).
# 2048 → 32768 (2026-08-29): with events filtered and slimmed, the count cap tripped on
# frames of ~500 bytes — a 10-worker teardown cancels every in-flight request at once and
# `Network.loadingFailed` arrived faster than the client parsed it (107,250 frames, 0.56 MB
# buffered, evicted 0.2 s before the run ended, marking its last records failed). The byte
# ceiling below is the memory bound; the count only needs to exceed a burst.
_PEER_OUTBOUND_FRAMES = max(1, int(os.environ.get("BH_PEER_OUTBOUND_FRAMES") or 32768))
_PEER_OUTBOUND_BYTES = max(
    MAX_DAEMON_FRAME + 1,  # one maximum legal line plus its newline delimiter
    int(os.environ.get("BH_PEER_OUTBOUND_BYTES") or (MAX_DAEMON_FRAME + 1)),
)


#: What a filtered client reads from the four Network events it subscribes to: the ids
#: `goto()` joins on, the type/status/error fields diagnostics keeps. Everything else —
#: request headers, POST bodies, the initiator stack trace, response headers/timing — is
#: dropped before fan-out. Measured 2026-08-29: a page issuing ~900 requests/s produced
#: 2.7 GB of `Network.requestWillBeSent` (23 KB each) in 131 s and evicted the client;
#: the same events slimmed are ~200 bytes.
_SLIM_NETWORK_KEYS = frozenset({
    "requestId", "loaderId", "frameId", "type", "timestamp", "wallTime", "errorText",
    "canceled", "blockedReason", "encodedDataLength", "hasUserGesture", "documentURL",
})
_SLIM_REQUEST_KEYS = frozenset({"url", "method"})
_SLIM_RESPONSE_KEYS = frozenset({"url", "status", "statusText", "mimeType", "protocol",
                                 "fromDiskCache", "fromServiceWorker"})


def _slim_event(msg: dict[str, Any]) -> dict[str, Any]:
    method = str(msg.get("method") or "")
    if not method.startswith("Network."):
        return msg
    params = msg.get("params")
    if not isinstance(params, dict):
        return msg
    slim = {k: v for k, v in params.items() if k in _SLIM_NETWORK_KEYS}
    request = params.get("request")
    if isinstance(request, dict):
        slim["request"] = {k: v for k, v in request.items() if k in _SLIM_REQUEST_KEYS}
    response = params.get("response")
    if isinstance(response, dict):
        slim["response"] = {k: v for k, v in response.items() if k in _SLIM_RESPONSE_KEYS}
    return {**msg, "params": slim}


def _encode_frame(payload: dict[str, Any]) -> bytes:
    """Encode one newline-delimited daemon frame without ASCII expansion."""
    return (json.dumps(
        payload, default=str, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8", errors="backslashreplace")


class _Peer:
    """One client socket with bounded, single-writer output ownership.

    Reply workers and the CDP reader only enqueue complete newline frames.  Exactly one
    peer-local writer calls ``sendall``, so framing and enqueue order are preserved without
    ever putting a slow client's socket on the browser reader thread.
    """

    __slots__ = (
        "_buffered", "_enqueued_bytes", "_enqueued_frames", "_filtered_frames", "_max_bytes",
        "_outbound", "_overflows", "_peak_bytes", "_peak_frames", "_sent_bytes",
        "_sent_frames", "_state_lock", "_writer", "closed", "methods", "sock",
    )

    _STOP = object()

    def __init__(self, sock: socket.socket, *, max_frames: int = _PEER_OUTBOUND_FRAMES,
                 max_bytes: int = _PEER_OUTBOUND_BYTES):
        self.sock = sock
        self.closed = threading.Event()
        #: Event filter negotiated at `subscribe`: None forwards every CDP event (the
        #: pre-2026-08-29 contract, kept for old clients). Measured on a 10-worker
        #: 100-posting run: unfiltered fan-out enqueued 265 MB / 39,638 frames in 41 s,
        #: overflowed the 2,048-frame queue and evicted the client mid-run. The client
        #: reads about eighteen event methods; `Network.responseReceivedExtraInfo` and
        #: `Network.dataReceived` — the bulk — are not among them.
        self.methods: tuple[frozenset[str], tuple[str, ...]] | None = None
        self._filtered_frames = 0
        self._outbound: queue.Queue[bytes | object] = queue.Queue(maxsize=max(1, max_frames))
        self._max_bytes = max(1, max_bytes)
        self._buffered = 0
        self._enqueued_frames = 0
        self._enqueued_bytes = 0
        self._sent_frames = 0
        self._sent_bytes = 0
        self._peak_frames = 0
        self._peak_bytes = 0
        self._overflows = 0
        self._state_lock = threading.Lock()
        self._writer = threading.Thread(
            target=self._write, name="bh-daemon-peer-writer", daemon=True)
        self._writer.start()

    def send(self, payload: dict[str, Any]) -> bool:
        line = _encode_frame(payload)
        overflow = False
        with self._state_lock:
            if self.closed.is_set():
                return False
            if self._buffered + len(line) > self._max_bytes:
                overflow = True
            else:
                try:
                    self._outbound.put_nowait(line)
                except queue.Full:
                    overflow = True
                else:
                    # Includes the frame while sendall is in progress.  A writer blocked
                    # in the kernel therefore still consumes the peer's byte budget.
                    self._buffered += len(line)
                    self._enqueued_frames += 1
                    self._enqueued_bytes += len(line)
                    self._peak_frames = max(self._peak_frames, self._outbound.qsize())
                    self._peak_bytes = max(self._peak_bytes, self._buffered)
                    return True
        if overflow:
            with self._state_lock:
                self._overflows += 1
            self.close()
            return False

    def _write(self) -> None:
        while True:
            # The timeout is a teardown backstop for the full-queue case: close() cannot
            # insert its sentinel when every slot is occupied, but the closed flag still
            # makes this writer terminate instead of eventually blocking on an empty queue.
            if self.closed.is_set():
                return
            try:
                frame = self._outbound.get(timeout=0.1)
            except queue.Empty:
                continue
            if frame is self._STOP:
                return
            assert isinstance(frame, bytes)
            if self.closed.is_set():
                with self._state_lock:
                    self._buffered -= len(frame)
                return
            try:
                self.sock.sendall(frame)
                with self._state_lock:
                    self._sent_frames += 1
                    self._sent_bytes += len(frame)
            except OSError:
                self.close()
                return
            finally:
                with self._state_lock:
                    self._buffered -= len(frame)

    def stats(self) -> dict[str, int]:
        """Privacy-safe transport pressure for diagnosing retries and disconnects."""
        with self._state_lock:
            return {
                "enqueued_frames": self._enqueued_frames,
                "enqueued_bytes": self._enqueued_bytes,
                "sent_frames": self._sent_frames,
                "sent_bytes": self._sent_bytes,
                "peak_frames": self._peak_frames,
                "peak_bytes": self._peak_bytes,
                "overflows": self._overflows,
                "filtered_frames": self._filtered_frames,
                "buffered_bytes": self._buffered,
                "queued_frames": self._outbound.qsize(),
            }

    def close(self) -> None:
        with self._state_lock:
            if self.closed.is_set():
                return
            self.closed.set()
            with contextlib.suppress(queue.Full):
                self._outbound.put_nowait(self._STOP)
        # shutdown is what releases a writer already blocked in sendall; close alone is
        # not guaranteed to wake a syscall running in another thread on every platform.
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        try:
            self.sock.close()
        except OSError:
            pass


class Daemon:
    """Serves the IPC socket. Owns exactly one `Connection` and one `SessionRegistry`."""

    def __init__(self, name: str, transport: Any, *, journal: Journal | None = None,
                 token: str | None = None, linger: float = DEATH_LINGER,
                 browser_identity: BrowserIdentity | None = None,
                 trace_cdp: bool = True):
        self.name = ipc.check_name(name)
        self.journal = journal or Journal(None)
        # A callable defers the handshake until after the endpoint is published; a live
        # transport is used as-is, which is what every unit test passes.
        self._make_transport = transport if callable(transport) else None
        # A foreground daemon and every short-lived client can inherit the same
        # ``BH_JOURNAL`` path. Tracing here as well as at the client boundary writes every
        # protocol round trip twice and turns telemetry into measurable I/O. Directly
        # constructed daemons retain their traced default for tests and embedders; the
        # foreground server disables only that duplicate protocol stream.
        cdp_journal = self.journal if trace_cdp else Journal(None)
        self.conn = Connection(None if self._make_transport else transport,
                               journal=cdp_journal)
        self.sessions = SessionRegistry(self.conn, journal=self.journal)
        self._token = token
        self._browser_identity = browser_identity
        self._server: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._peers: set[_Peer] = set()
        self._plock = threading.Lock()
        # A lease is deliberately an opaque capability, not another daemon-wide
        # "current tab" cursor.  It lets a later client rebind to one explicit target
        # without making concurrent clients fight over implicit shared state.
        self._leases: dict[str, str] = {}
        self._lease_for_target: dict[str, str] = {}
        self._lease_lock = threading.Lock()
        # Which client (peer connection) has ADOPTED which target as its default tab.
        # This exists because the ergonomic no-target fallback used to be computed
        # client-side — list drivable pages, take the first — and two fresh clients
        # against one browser therefore both took the SAME page and clobbered each
        # other's navigations (the exact bug browser-use PR 618 fixes in v1, one layer
        # down). The daemon is the only place the choice can be made atomically. An
        # adoption is not a lease: it is advisory, scoped to the fallback only —
        # explicit `use_tab(target_id)` is untouched — and dies with the client's
        # connection rather than needing release calls.
        self._adoptions: dict[_Peer, str] = {}
        self._adopt_lock = threading.Lock()
        # Page target metadata is already streamed by Target.setDiscoverTargets. Retaining
        # it lets the next short-lived client reuse an attached page without issuing
        # Target.getTargets merely to rediscover the same target. Lifecycle events remove
        # dead/non-drivable entries; reservations are still decided under _adopt_lock.
        self._page_infos: dict[str, dict[str, Any]] = {}
        self._target_info_lock = threading.Lock()
        #: Set once the browser handshake has *finished*, successfully or not. The IPC
        #: endpoint is published before this, so a client can always reach the daemon and
        #: be told what is pending instead of waiting out a silent timeout.
        self._settled = threading.Event()
        self._connect_error = ""
        self._connect_started = False
        #: An argument, never an env var (§6) — the tests need a short one.
        self._linger = linger
        self._died_at: float | None = None
        self._request_pool = ThreadPoolExecutor(max_workers=_DAEMON_WORKERS,
                                                thread_name_prefix="bh-daemon-req")
        self._admission = threading.BoundedSemaphore(_DAEMON_QUEUE)

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
        # The sheet cannot be pressed before the handshake starts (it does not exist yet)
        # or after it returns (it is already answered), only alongside it. No-op off macOS
        # and under BH_MAC_APPROVE=0.
        #
        # Guarded, and deliberately so: this sat outside the try below, so a raise from the
        # import or from arm() skipped the `finally` and left `_settled` unset — which makes
        # `_browser_is_dead` False forever, so `_expired` never fires and the daemon wedges
        # answering `connecting: true`. A convenience must never be able to do that.
        approval_stop = threading.Event()
        handshake_pending = threading.Event()
        try:
            if self._make_transport is not None:
                # THIS is the call Chrome blocks: the websocket handshake waits on the
                # consent prompt. It happens here, after bind(), so the daemon is already
                # answering pings and can report that it is waiting.
                try:
                    from harness.connect import macos
                    macos.arm(
                        approval_stop,
                        identity=self._browser_identity,
                        pending=handshake_pending,
                    )
                except Exception as e:               # noqa: BLE001 — noted, never fatal
                    self.journal.write(
                        "daemon", event="mac_approve_unavailable", error=str(e)[:200])
                handshake_pending.set()
                try:
                    self.conn.attach(self._make_transport())
                finally:
                    # This exact constructor either returned or raised.  The sidecar has
                    # no authority to inspect UI after that handshake ceased to be pending.
                    handshake_pending.clear()
            self.conn.start()
            self.conn.subscribe(self._watch_disconnect)
            self.conn.subscribe(self._watch_leases)
            self.conn.subscribe(self._watch_target_infos)
            self.conn.subscribe(self._broadcast)
            self.sessions.discover()
        except Exception as e:                       # noqa: BLE001 — reported, not raised
            self._connect_error = f"{type(e).__name__}: {str(e)[:200]}"
            self.journal.write("daemon", event="connect_failed", error=self._connect_error)
        finally:
            approval_stop.set()
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
        if self.conn.closed:
            # Rule 1: say what was observed. "The websocket this daemon holds is gone" is
            # verified; letting the call fall through to `ready_session` would have reported
            # whatever class the registry happened to raise first instead.
            return fail(Class.BROWSER_DISCONNECTED,
                        "browser connection dropped — this daemon is shutting down so a "
                        "fresh one can reconnect", daemon=self.name).to_json()
        return None

    def serve_forever(self) -> None:
        assert self._server is not None, "call start() first"
        while not self._stop.is_set():
            if self._expired():
                # Recovery is by EXIT, not by reconnect (finding C, option b).
                #
                # Reconnecting in place looked cheaper and is not. The handshake is a fresh
                # `WebSocketTransport`, and Chrome prompts per websocket (D7) — a background
                # retry loop would raise modals at a user who is not there, and cannot tell
                # "Chrome restarted" from "the user said no". Worse, every session id in the
                # registry, every lease and every adoption is void the moment the socket
                # dies, so a reconnect would have to re-run the whole cold-start path from a
                # second, rarely-exercised branch. Exiting reuses the spawn path
                # `ensure_daemon` takes every day: the socket is unlinked, `ipc.ping` returns
                # None, and the next client spawns a daemon that handshakes once, arms the
                # macOS sheet presser once, and starts from clean state. The wedge this
                # replaces was permanent — the name answered forever with `browser: false`
                # and only a manual kill freed it.
                self.journal.write("daemon", event="exit_browser_dead",
                                   reason=self._connect_error or "cdp connection closed")
                self.stop()
                return
            try:
                client, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            thread = threading.Thread(target=self._serve_client, args=(client,), daemon=True)
            thread.start()
            self._threads = [existing for existing in self._threads if existing.is_alive()]
            self._threads.append(thread)

    def _browser_is_dead(self) -> bool:
        """Settled, and the browser is not coming back **on this process**.

        Not settled is not dead: the handshake blocks on Chrome's consent prompt for as long
        as the user takes to answer it, and killing the daemon out from under a click that is
        still pending is how the pending case would turn back into silence.
        """
        if not self._connect_started:
            return False        # conn is driven directly (unit tests): nothing to judge
        if not self._settled.is_set():
            return False
        return bool(self._connect_error) or self.conn.closed

    def _expired(self) -> bool:
        """True once the browser has been dead for longer than the linger window."""
        if not self._browser_is_dead():
            self._died_at = None            # a daemon only dies once; nothing to hold open
            return False
        if self._died_at is None:
            self._died_at = time.monotonic()
            self.journal.write("daemon", event="browser_dead",
                               reason=self._connect_error or "cdp connection closed",
                               linger=self._linger)
            return False
        return (time.monotonic() - self._died_at) >= self._linger

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
        with self._plock:
            peers = list(self._peers)
            self._peers.clear()
        for peer in peers:
            peer.close()
        self._request_pool.shutdown(wait=False, cancel_futures=True)
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
        the client's `rid`, `_Peer.send` atomically enqueues complete frames for its sole
        writer, and the CDP connection below already multiplexes.
        """
        peer = _Peer(client)
        buf = bytearray()
        def answer_and_send(line: bytes) -> None:
            try:
                if peer.closed.is_set():
                    return
                reply = self._answer(line, peer)
                # Echo the client's request id so a multiplexed client can match the
                # reply to the call, the same way CDP's own `id` works.
                if (rid := _rid_of(line)) is not None:
                    reply = {**reply, "rid": rid}
                peer.send(reply)
            except OSError:
                pass        # the peer went away mid-reply; the read loop will notice
            finally:
                self._admission.release()

        try:
            while not self._stop.is_set():
                chunk = client.recv(1 << 16)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, _, rest = bytes(buf).partition(b"\n")
                    buf = bytearray(rest)
                    if not self._admission.acquire(blocking=False):
                        reply = fail(
                            Class.RESOURCE_LIMIT,
                            "daemon request admission queue is full",
                            active_limit=_DAEMON_WORKERS, queue_limit=_DAEMON_QUEUE,
                        ).to_json()
                        if (rid := _rid_of(line)) is not None:
                            reply["rid"] = rid
                        peer.send(reply)
                        continue
                    try:
                        self._request_pool.submit(answer_and_send, line)
                    except RuntimeError:
                        self._admission.release()
                        return
        except OSError:
            return          # a client that vanishes mid-request is normal, not an error
        finally:
            # Mark the peer closed before adoption cleanup. An in-flight adopt request no
            # longer holds `_adopt_lock` across CDP, so it can otherwise reserve a target
            # in the tiny gap after cleanup found no mapping but before close was visible.
            peer.close()
            self.journal.write("daemon", event="client_closed", **peer.stats())
            with self._plock:
                self._peers.discard(peer)
            with self._adopt_lock:
                # The client is gone; its default tab returns to the adoptable pool. The
                # TAB stays open — closing it is Session.close_tab's job, never implied.
                self._adoptions.pop(peer, None)

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
            spec = request.get("methods")
            if isinstance(spec, dict):
                exact = frozenset(str(m) for m in (spec.get("exact") or []) if m)
                prefixes = tuple(str(p) for p in (spec.get("prefixes") or []) if p)
                peer.methods = (exact, prefixes) if (exact or prefixes) else None
            with self._plock:
                self._peers.add(peer)
            return _value(ok({"subscribed": True, "protocol": PROTOCOL_VERSION,
                              "version": VERSION, "filtered": peer.methods is not None}))
        return self.handle(request, peer=peer)

    def _broadcast(self, msg: dict[str, Any]) -> None:
        """Fan a CDP event out to subscribed clients. Runs on the CDP reader thread, so it
        must never block: a peer that has gone away is dropped, not waited on."""
        with self._plock:
            peers = list(self._peers)
        if not peers:
            return
        frame = {"event": msg}
        slim_frame: dict[str, Any] | None = None
        method = str(msg.get("method") or "")
        for peer in peers:
            spec = peer.methods
            if spec is not None and method not in spec[0] and not method.startswith(spec[1]):
                peer._filtered_frames += 1
                continue
            if spec is not None:
                if slim_frame is None:
                    slim_frame = {"event": _slim_event(msg)}
                out = slim_frame
            else:
                out = frame
            if not peer.send(out):
                stats = peer.stats()
                # A send fails for two unrelated reasons: the queue overflowed (eviction —
                # the reader fell behind) or the client had already closed its socket (it
                # finished). Both used to be logged as `peer_evicted`.
                self.journal.write(
                    "daemon", event="peer_evicted" if stats.get("overflows") else "peer_gone",
                    method=msg.get("method"), **stats)
                with self._plock:
                    self._peers.discard(peer)

    def handle(self, request: dict[str, Any],
               peer: _Peer | None = None) -> dict[str, Any]:
        """Answer one request. Always an outcome, never a bare value or a bare string."""
        meta = request.get("meta")
        if meta:
            return self._meta(meta, request, peer=peer)
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
                # The client journals this exact forwarded method and helper parent. The
                # daemon journal is reserved for browser calls the daemon adds itself
                # (attach/domain/runtime preparation), yielding one record per real call.
                trace=False,
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

    def _meta(self, meta: str, request: dict[str, Any],
              peer: _Peer | None = None) -> dict[str, Any]:
        if meta == "ping":
            # Liveness means *both* processes are alive: a meta-only pong from a daemon whose
            # browser socket is dead is what v1 needed six PRs to stop reporting as healthy.
            # So `browser` stays the readiness signal — but the pong now also carries WHY it
            # is false, which is the difference between "still waiting on your click" and
            # "never coming up".
            settled = self._settled.is_set() or not self._connect_started
            # `conn.closed`, not `conn._closed`: until the reader started writing that flag
            # this line could only ever see a *locally* closed connection, so a browser that
            # quit or crashed left the pong reading `browser: true` — the very report the
            # comment above claims was fixed.
            live = settled and not self._connect_error and not self.conn.closed
            out: dict[str, Any] = {"pong": True, "browser": live,
                                   "protocol": PROTOCOL_VERSION, "version": VERSION,
                                   "targets": self.sessions.live_targets}
            if not live:
                out["connecting"] = not settled
                out["reason"] = self._connect_error or (
                    "browser handshake still open — Chrome prompts per websocket and "
                    "blocks until it is answered" if not settled
                    else "browser connection dropped — this daemon is exiting so the next "
                         "client can spawn one that reconnects")
            return out
        if (pending := self._browser_pending(20.0)) is not None and meta in (
                "attach", "prepare_runtime", "ensure_domains", "forget", "adopt",
                "lease_create", "lease_claim", "lease_release"):
            return pending
        if meta == "adopt":
            # The ergonomic no-target fallback, decided HERE so it can be decided once.
            # Client-side each fresh client listed the drivable pages and took the first,
            # so two clients starting against one browser took the same page and their
            # navigations clobbered each other. Under the lock: the first unadopted
            # drivable page, else a fresh BACKGROUND tab — a second client never steals
            # the page the first is working in, and never yanks the user's focus either.
            exclude = {str(t) for t in (request.get("exclude") or [])}
            try:
                pick = None
                created = False
                cache_hit = False
                # Only reuse targets with a live registered session. That makes a missed
                # target event degrade to the old discovery path instead of handing a new
                # client a stale id.
                live = set(self.sessions.live_targets)
                with self._adopt_lock, self._lease_lock:
                    taken = set(self._adoptions.values()) | set(self._lease_for_target) | exclude
                    with self._target_info_lock:
                        pick = next(
                            (
                                target_id
                                for target_id, info in self._page_infos.items()
                                if target_id in live
                                and target_id not in taken
                                and info.get("type") == "page"
                                and str(info.get("url", "")).startswith(_DRIVABLE)
                            ),
                            None,
                        )
                    if pick is not None:
                        if peer is not None and not peer.closed.is_set():
                            self._adoptions[peer] = pick
                        cache_hit = True

                # Never hold either state lock across a CDP round trip. Event subscribers
                # run on the one CDP reader thread, and `_watch_leases` needs `_lease_lock`;
                # blocking that reader while waiting for its reply deadlocks until timeout.
                # The target list may be stale by the time we lock, but `taken` is current,
                # so concurrent leases/adoptions still cannot select the same target.
                while pick is None:
                    infos = self.conn.request("Target.getTargets", timeout=10.0) \
                        .get("targetInfos") or []
                    self._remember_target_infos(infos)
                    with self._adopt_lock, self._lease_lock:
                        # An opaque lease reserves its target from all implicit adoption.
                        # Selection and reservation are one atomic state transition.
                        taken = (set(self._adoptions.values())
                                 | set(self._lease_for_target) | exclude)
                        pick = next(
                            (t["targetId"] for t in infos
                             if t.get("type") == "page"
                             and str(t.get("url", "")).startswith(_DRIVABLE)
                             and t["targetId"] not in taken),
                            None)
                        if pick is not None:
                            if peer is not None and not peer.closed.is_set():
                                self._adoptions[peer] = pick
                            created = False
                            break

                    # No existing target was available. Creation must also happen without
                    # the state locks: Target.targetDestroyed can race any CDP reply. A
                    # lease can claim the new id after Chrome announces it, so reserve it
                    # under both locks and retry if that rare race was won elsewhere.
                    pick = self.conn.request(
                        "Target.createTarget",
                        {"url": "about:blank", "background": True},
                        timeout=10.0)["targetId"]
                    with self._adopt_lock, self._lease_lock:
                        taken = (set(self._adoptions.values())
                                 | set(self._lease_for_target) | exclude)
                        if pick in taken:
                            continue
                        if peer is not None and not peer.closed.is_set():
                            self._adoptions[peer] = pick
                        created = True
                        self._remember_target_infos(
                            [{"targetId": pick, "type": "page", "url": "about:blank"}]
                        )
                        break
            except HarnessError as e:
                return e.outcome.to_json()
            self.journal.write("daemon", event="target_adopted", target_id=pick,
                               created=created, cache_hit=cache_hit)
            return _value(ok({"target_id": pick, "created": created}))
        if meta == "attach":
            try:
                return _value(ok(self.sessions.ready_session(request["target_id"]).to_json()))
            except HarnessError as e:
                return e.outcome.to_json()
        if meta == "prepare_runtime":
            try:
                session = self.sessions.prepare_runtime(request["target_id"])
                return _value(ok(session.to_json()))
            except HarnessError as e:
                return e.outcome.to_json()
        if meta == "ensure_domains":
            domains = tuple(
                str(domain) for domain in (request.get("domains") or []) if domain
            )
            try:
                session = self.sessions.ensure_domains(request["target_id"], domains)
                return _value(ok(session.to_json()))
            except HarnessError as e:
                return e.outcome.to_json()
        if meta == "forget":
            self.sessions.forget(request["target_id"])
            return _value(ok(None))
        if meta == "lease_create":
            target_id = str(request.get("target_id") or "")
            if not target_id:
                return fail(Class.CDP_ERROR, "lease_create needs a target_id").to_json()
            try:
                # Validate before minting: a lease must never turn a dead target into a
                # future implicit fallback.
                self.sessions.ensure_live(target_id)
            except HarnessError as e:
                return e.outcome.to_json()
            with self._lease_lock:
                if target_id in self._lease_for_target:
                    return fail(Class.SCOPE_REFUSED, "target already has an active lease",
                                target_id=target_id).to_json()
                lease = secrets.token_urlsafe(24)
                self._leases[lease] = target_id
                self._lease_for_target[target_id] = lease
            self.journal.write("daemon", event="lease_created", target_id=target_id)
            return _value(ok({"lease": lease, "target_id": target_id}))
        if meta == "lease_claim":
            lease = str(request.get("lease") or "")
            with self._lease_lock:
                target_id = self._leases.get(lease)
            if target_id is None:
                return fail(Class.SCOPE_REFUSED, "unknown or expired target lease").to_json()
            try:
                self.sessions.ensure_live(target_id)
            except HarnessError:
                self._drop_lease(lease)
                return fail(Class.SCOPE_REFUSED,
                            "unknown or expired target lease").to_json()
            return _value(ok({"target_id": target_id}))
        if meta == "lease_release":
            lease = str(request.get("lease") or "")
            if not self._drop_lease(lease):
                return fail(Class.SCOPE_REFUSED, "unknown or expired target lease").to_json()
            return _value(ok(None))
        return fail(Class.CDP_ERROR, f"unknown meta {meta!r}").to_json()

    # -- events ------------------------------------------------------------

    def _watch_disconnect(self, msg: dict[str, Any]) -> None:
        if msg.get("method") == "Inspector.detached":
            self.sessions.disconnected(str((msg.get("params") or {}).get("reason", "")))

    def _watch_leases(self, msg: dict[str, Any]) -> None:
        """Forget capabilities for targets Chrome says are gone."""
        if msg.get("method") not in ("Target.targetDestroyed", "Target.targetCrashed"):
            return
        target_id = str((msg.get("params") or {}).get("targetId") or "")
        if not target_id:
            return
        with self._lease_lock:
            lease = self._lease_for_target.pop(target_id, None)
            if lease is not None:
                self._leases.pop(lease, None)

    def _watch_target_infos(self, msg: dict[str, Any]) -> None:
        """Maintain the drivable-page cache from the lifecycle stream already enabled."""
        method = msg.get("method")
        params = msg.get("params") or {}
        if method in ("Target.targetCreated", "Target.targetInfoChanged"):
            info = params.get("targetInfo") or {}
            self._remember_target_infos([info])
            return
        if method not in ("Target.targetDestroyed", "Target.targetCrashed"):
            return
        target_id = str(params.get("targetId") or "")
        if target_id:
            with self._target_info_lock:
                self._page_infos.pop(target_id, None)

    def _remember_target_infos(self, infos: list[dict[str, Any]]) -> None:
        with self._target_info_lock:
            for info in infos:
                target_id = str(info.get("targetId") or "")
                if not target_id:
                    continue
                if info.get("type") == "page" and str(info.get("url", "")).startswith(
                    _DRIVABLE
                ):
                    self._page_infos[target_id] = dict(info)
                else:
                    self._page_infos.pop(target_id, None)

    def _drop_lease(self, lease: str) -> bool:
        with self._lease_lock:
            target_id = self._leases.pop(lease, None)
            if target_id is None:
                return False
            self._lease_for_target.pop(target_id, None)
        self.journal.write("daemon", event="lease_released", target_id=target_id)
        return True


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
    journal = (
        Journal(journal_path, session=name, cdp_origin="daemon_internal")
        if journal_path
        else Journal(None)
    )
    # Topology, not the URL. A journal is written to be read later and shared — `bh stats`
    # and `bh trace` exist for exactly that — and `telemetry.py` states the contract this
    # line was breaking: URLs are never selected into it in the first place. The ws path is
    # a capability, so "which browser did it find" has to be answered without handing over
    # the means to drive it.
    journal.write("daemon", event="serving", ws=safe_endpoint(resolution.ws_url),
                  strategy=resolution.strategy)
    # A factory, not a live transport: constructing WebSocketTransport performs the
    # handshake, and doing that before Daemon.start() meant the port file appeared only
    # after Chrome's consent prompt was answered.
    daemon = Daemon(name, lambda: WebSocketTransport(resolution.ws_url),
                    journal=journal, browser_identity=resolution.identity).start()
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
