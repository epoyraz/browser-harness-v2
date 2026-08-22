"""Connection tests: id-multiplexing, typed errors, and the single prose boundary."""
import base64
import hashlib
import socket
import threading
import time

import pytest

from harness.connect.cdp import Connection, WebSocketTransport, classify
from harness.core.outcome import BrowserDisconnected, Class, HarnessError, Timeout
from tests.fake_browser import FakeBrowser


@pytest.fixture
def conn():
    c = Connection(FakeBrowser("a", "b")).start()
    yield c
    c.close()


# --- multiplexing ------------------------------------------------------------

def test_replies_route_to_the_thread_that_asked(conn):
    """The fake answers from worker threads, so replies arrive out of order by construction."""
    results, lock = {}, threading.Lock()

    def ask(i):
        r = conn.request("Runtime.evaluate", {"expression": f"e{i}"})
        with lock:
            results[i] = r

    threads = [threading.Thread(target=ask, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(results) == 16


def test_requests_overlap_rather_than_queueing():
    """TODO 7's done-when, at the connection layer: one websocket must not serialise callers.

    Eight 200 ms calls take ~200 ms, not 1.6 s. v1's IPC answered one request at a time, so
    a subagent's slow call blocked every other client behind it.
    """
    browser = FakeBrowser("a", latency=0.2)
    with Connection(browser) as c:
        started = time.monotonic()
        threads = [threading.Thread(target=lambda: c.request("Runtime.evaluate"))
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"8 concurrent 200ms calls took {elapsed:.2f}s — they serialised"
    assert browser.max_in_flight > 1


# --- typed errors ------------------------------------------------------------

def test_a_cdp_error_becomes_its_typed_class(conn):
    with pytest.raises(HarnessError) as e:
        conn.request("Target.attachToTarget", {"targetId": "ghost", "flatten": True})
    assert e.value.cls is Class.TARGET_GONE
    assert e.value.observed["code"] == -32000


def test_an_unrecognised_error_is_cdp_error_not_a_guess():
    """Rule 1: never invent a cause. An unmapped message must not become `SESSION_STALE`."""
    assert classify({"message": "Some entirely new Chrome wording"}) is Class.CDP_ERROR


def test_classify_covers_the_wordings_that_cost_v1_bugs():
    assert classify({"message": "Session with given id not found."}) is Class.SESSION_STALE
    assert classify({"message": "No target with given id found: 1A2B"}) is Class.TARGET_GONE
    assert classify({"message": "Target closed."}) is Class.TARGET_GONE
    assert classify({"message": "Could not find node with given id"}) is Class.ELEMENT_GONE


def test_timeout_is_typed_and_names_the_method():
    # The fake must be genuinely slower than the timeout: with `timeout=0.0` against a
    # zero-latency fake, the reply can land between send and wait, and no timeout fires.
    browser = FakeBrowser("a", latency=0.5)
    with Connection(browser) as c, pytest.raises(Timeout) as e:
        c.request("Runtime.evaluate", timeout=0.05)
    assert e.value.observed["method"] == "Runtime.evaluate"
    assert e.value.retryable is True


def test_a_timed_out_reply_arriving_late_is_discarded():
    """A late reply whose caller has gone must not be routed into the next caller's slot.

    The latency is what makes this deterministic: a `timeout=0` version raced the reader
    thread and failed roughly one full-suite run in two.
    """
    browser = FakeBrowser("a", latency=0.3)
    with Connection(browser) as c:
        with pytest.raises(Timeout):
            c.request("Runtime.evaluate", {"expression": "abandoned"}, timeout=0.05)
        time.sleep(0.4)                      # the abandoned reply lands here, unclaimed
        browser.latency = 0.0
        assert c.request("Runtime.evaluate", {"expression": "next"})["result"]["echo"] == "next"


# --- disconnection -----------------------------------------------------------

def test_a_dropped_connection_wakes_every_waiter():
    """Without this, losing the browser turns each in-flight call into a full-timeout hang,
    which is how a dead connection used to read as slowness."""
    browser = FakeBrowser("a", latency=5.0)
    c = Connection(browser).start()
    errors = []

    def ask():
        try:
            c.request("Runtime.evaluate", timeout=30.0)
        except HarnessError as e:
            errors.append(e)

    threads = [threading.Thread(target=ask) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.1)
    browser.close()
    for t in threads:
        t.join(5)
    assert len(errors) == 3
    assert all(isinstance(e, BrowserDisconnected) for e in errors)


def test_requesting_on_a_closed_connection_fails_fast():
    c = Connection(FakeBrowser("a")).start()
    c.close()
    with pytest.raises(BrowserDisconnected):
        c.request("Runtime.evaluate")


class _DeafTransport:
    """A socket that takes writes and will never answer.

    The shape of a dropped connection as the *writer* sees it: `send` succeeds into the
    void while the reader is already at EOF. A FakeBrowser cannot model this — its `send`
    raises once it is closed, which hides the hang behind an accidental fast failure.
    """

    def __init__(self, error: BaseException | None = None):
        self.sent: list[dict] = []
        self.closed = False
        self.read_failed = threading.Event()
        self._error = error or EOFError("browser closed the connection")

    def send(self, msg):
        self.sent.append(msg)

    def recv(self, timeout=None):
        self.read_failed.set()
        raise self._error

    def close(self):
        self.closed = True


def test_a_reader_that_dies_closes_the_connection_and_fails_the_next_call_fast():
    """Neither terminal path in `_pump` used to mark the connection closed, so after Chrome
    quit the reader thread was gone while the object still called itself open: the next call
    wrote into the void and waited out its whole per-call timeout."""
    t = _DeafTransport()
    c = Connection(t).start()
    assert t.read_failed.wait(2), "the reader never reached its terminal path"

    started = time.monotonic()
    with pytest.raises(BrowserDisconnected) as e:
        c.request("Runtime.evaluate", timeout=5.0)
    elapsed = time.monotonic() - started

    assert e.value.cls is Class.BROWSER_DISCONNECTED
    assert elapsed < 1.0, f"the call waited {elapsed:.2f}s — it hung on the per-call timeout"
    assert t.sent == [], "a closed connection must not write into the void"
    assert c.closed is True


def test_a_reader_killed_by_an_unexpected_error_also_closes_the_connection():
    """The blanket `except` is a terminal path too. A frame the decoder cannot handle kills
    the reader just as finally as EOF does, and used to leave the same open-looking corpse."""
    c = Connection(_DeafTransport(ValueError("frame decoder blew up"))).start()
    _settle(lambda: c.closed)
    with pytest.raises(BrowserDisconnected):
        c.request("Runtime.evaluate", timeout=5.0)


def test_a_waiter_woken_by_the_dying_reader_finds_the_connection_already_closed():
    """Ordering, not just eventual consistency: the flag is set before the waiters are woken.
    A caller that retries the instant its call fails must get the typed fast failure rather
    than sailing into another full-timeout wait against a reader that is already gone."""
    browser = FakeBrowser("a", latency=5.0)
    c = Connection(browser).start()
    seen: dict = {}

    def ask():
        try:
            c.request("Runtime.evaluate", timeout=30.0)
        except HarnessError as e:
            seen["error"] = e
            seen["closed_when_woken"] = c.closed

    thread = threading.Thread(target=ask)
    thread.start()
    time.sleep(0.1)
    browser.close()
    thread.join(5)

    assert isinstance(seen.get("error"), BrowserDisconnected)
    assert seen["closed_when_woken"] is True


def test_closing_after_the_reader_died_still_shuts_the_transport_down():
    """`_closed` cannot double as the "teardown already ran" latch once the reader writes it:
    `close()` would short-circuit and leave the socket open for the life of the daemon — and
    the daemon calls `close()` on exactly this path."""
    t = _DeafTransport()
    c = Connection(t).start()
    _settle(lambda: c.closed)
    c.close()
    assert t.closed is True


def test_a_vanished_reader_thread_reports_the_connection_closed():
    """Belt and braces for what `_pump`'s excepts cannot catch — a reader that ended without
    running either terminal path. A connection with no reader can never complete a request,
    so reporting it open is the healthy-corpse answer `ping` exists to prevent."""
    c = Connection(FakeBrowser("a"))
    assert c.closed is False, "a connection that was never started has no dead reader"

    finished = threading.Thread(target=lambda: None)
    finished.start()
    finished.join()
    c._reader = finished
    assert c.closed is True


# --- events ------------------------------------------------------------------

def test_events_reach_subscribers(conn):
    seen = []
    conn.subscribe(seen.append)
    conn.request("Target.attachToTarget", {"targetId": "a", "flatten": True})
    _settle(lambda: any(e.get("method") == "Target.attachedToTarget" for e in seen))


def test_one_failing_handler_does_not_kill_the_reader(conn):
    """Observability must never break the run — a bad subscriber cannot deafen the others."""
    seen = []
    conn.subscribe(lambda _: (_ for _ in ()).throw(ValueError("bad handler")))
    conn.subscribe(seen.append)
    conn.request("Target.attachToTarget", {"targetId": "a", "flatten": True})
    _settle(lambda: seen)
    assert conn.request("Runtime.evaluate")          # reader still alive


def _settle(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("event never arrived")


# --- keepalive pings -----------------------------------------------------------
# A busy browser (or a slow cloud link) can leave a ws ping unanswered longer than
# ping_timeout; the library then force-closes with `1011 keepalive ping timeout` —
# indistinguishable from a real disconnect, and fatal here because one websocket
# serves every client. Reproduced live against a never-ponging endpoint before the
# fix (same root cause as cdp-use PR #25 / browser-use#4688).

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class _SilentServer:
    """RFC6455 upgrade handshake, then total silence: no reads, so no pongs ever."""

    def __init__(self):
        self._srv = socket.socket()
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self._done = threading.Event()
        self._conns: list[socket.socket] = []
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self._srv.getsockname()[1]}"

    def close(self) -> None:
        self._done.set()
        try:
            self._srv.close()
        except OSError:
            pass

    def _serve(self):
        self._srv.settimeout(0.25)
        while not self._done.is_set():
            try:
                conn, _ = self._srv.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self._conns.append(conn)
            threading.Thread(target=self._hold, args=(conn,), daemon=True).start()

    def _hold(self, conn: socket.socket):
        try:
            conn.settimeout(2.0)
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                req += chunk
            key = next(
                line.split(":", 1)[1].strip()
                for line in req.decode("latin1").split("\r\n")
                if line.lower().startswith("sec-websocket-key:")
            )
            accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
            conn.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode()
            )
        except OSError:
            return
        self._done.wait(30)                  # silence — but closable from the test
        try:
            conn.close()
        except OSError:
            pass


def test_transport_defaults_to_no_keepalive_pings(monkeypatch):
    """The default must not inherit websockets' `ping_interval=20`: that config was
    measured force-closing the connection with 1011 against a browser that merely
    stayed busy past 40 s."""
    captured: dict = {}

    class _FakeWS:
        def send(self, msg): ...
        def recv(self, timeout=None): raise TimeoutError()
        def close(self): ...

    def fake_connect(url, **kwargs):
        captured.update(kwargs)
        return _FakeWS()

    monkeypatch.setattr("websockets.sync.client.connect", fake_connect)
    WebSocketTransport("ws://127.0.0.1:1")
    assert captured["ping_interval"] is None


@pytest.mark.slow
def test_keepalive_ping_timeout_closes_a_silent_connection():
    """Control for the kwarg assertion above: with pings ON, the silent endpoint really
    does produce the 1011 EOFError — proving the mechanism, not just the absence of pings."""
    server = _SilentServer()
    t = WebSocketTransport(server.url, ping_interval=0.5, ping_timeout=0.5)
    try:
        deadline = time.monotonic() + 25   # close_timeout=10 can stretch the teardown
        while time.monotonic() < deadline:
            try:
                t.recv(timeout=1)
            except TimeoutError:
                continue
            except EOFError as e:
                assert "keepalive" in str(e)
                return
        pytest.fail("connection survived a silent browser despite keepalive pings")
    finally:
        t.close()
        server.close()


# -- the reader's death, tested so the fix cannot be deleted silently ---------

def test_reader_death_sets_the_closed_flag_itself_not_just_the_liveness_fallback():
    """Discriminates the fix from the safety net behind it.

    `_dead()` has two signals: the `_closed` flag, and a reader-thread liveness check. A
    mutation that drops `closing=True` from `_reader_stopped` still passes any test reading
    the `closed` PROPERTY, because by then the thread has ended and liveness answers. So
    this asserts the raw flag while the reader is still alive — the only assertion that
    fails if the headline fix is removed.
    """
    browser = FakeBrowser("a", "b")
    conn = Connection(browser).start()
    try:
        assert conn._closed is False
        assert conn._reader is not None and conn._reader.is_alive()

        conn._reader_stopped(BrowserDisconnected("chrome went away"))

        assert conn._closed is True, "the flag itself must be set, not inferred from liveness"
        assert conn._reader.is_alive(), "the reader is still up: liveness cannot be the signal"
    finally:
        conn.close()


def test_a_pending_request_is_failed_with_the_readers_cause_not_a_timeout():
    """Rule 2 — never discard a cause you were handed."""
    browser = FakeBrowser("a", "b")
    conn = Connection(browser).start()
    try:
        results: list[BaseException] = []

        def waiter():
            try:
                conn.request("Target.getTargets", timeout=10.0)
            except BaseException as e:      # noqa: BLE001 — recorded for the assert
                results.append(e)

        t = threading.Thread(target=waiter)
        t.start()
        deadline = time.monotonic() + 3
        while not conn._pending and time.monotonic() < deadline:
            time.sleep(0.01)
        assert conn._pending, "no in-flight request to fail"

        started = time.monotonic()
        conn._reader_stopped(BrowserDisconnected("chrome went away"))
        t.join(timeout=5)
        elapsed = time.monotonic() - started

        assert results and isinstance(results[0], HarnessError)
        assert results[0].outcome.cls is Class.BROWSER_DISCONNECTED
        assert elapsed < 1.0, f"waited {elapsed:.2f}s — it sat out the timeout instead"
    finally:
        conn.close()


def test_a_dispatch_failure_claims_the_connection_instead_of_killing_the_reader_silently():
    """The third terminal path.

    `_dispatch` sat outside the try, so a frame it could not handle killed the reader with
    `_closed` False and `_pending` never drained — every in-flight caller then waited out
    its full timeout. The next call recovers via the liveness fallback; the calls already
    waiting do not, which is why this path has to claim the connection itself.
    """
    browser = FakeBrowser("a", "b")
    conn = Connection(browser).start()
    try:
        boom = []

        def explode(_msg):
            boom.append(1)
            raise AttributeError("'list' object has no attribute 'get'")
        conn._dispatch = explode                       # a frame shaped wrong

        browser.emit("Page.lifecycleEvent", {"name": "load"})

        deadline = time.monotonic() + 5
        while not conn._closed and time.monotonic() < deadline:
            time.sleep(0.01)

        assert boom, "the dispatch was never reached"
        assert conn._closed is True, "a dispatch failure must claim the connection"
    finally:
        conn.close()
