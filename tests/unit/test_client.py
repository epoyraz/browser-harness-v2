"""Client-side daemon transport. These run a REAL daemon over a REAL unix socket against
the fake browser — the IPC layer is the thing under test, so mocking it would test nothing.
"""
import os
import threading
import time

import pytest

from harness.connect.client import RemoteConnection, RemoteRegistry, ensure_daemon
from harness.connect.daemon import Daemon
from harness.core.outcome import BrowserDisconnected, Class, HarnessError
from tests.fake_browser import FakeBrowser


@pytest.fixture
def runtime(monkeypatch):
    d = f"/tmp/bhc{os.getpid()}"
    os.makedirs(d, exist_ok=True)
    monkeypatch.setenv("BH_RUNTIME_DIR", d)
    return d


@pytest.fixture
def served(runtime):
    """A live daemon on a real socket, with a fake browser behind it."""
    browser = FakeBrowser("a", "b")
    daemon = Daemon("clienttest", browser).start()
    threading.Thread(target=daemon.serve_forever, daemon=True).start()
    yield browser, daemon
    daemon.stop()


def test_a_client_speaks_the_connection_interface(served):
    """`Tab` asks a connection for exactly three things. If the remote one provides them,
    every primitive works over IPC unchanged — that symmetry is the design."""
    _, _ = served
    with RemoteConnection("clienttest") as conn:
        assert conn.request("Runtime.evaluate", {"expression": "x"})["result"]["echo"] == "x"
        assert callable(conn.subscribe) and conn.journal is not None


def test_events_reach_the_client_through_the_daemon(served):
    """Without this the whole ops layer breaks over IPC: every wait, the dialog dance and
    the click delta are event-driven."""
    browser, _ = served
    seen = []
    with RemoteConnection("clienttest") as conn:
        conn.subscribe(seen.append)
        conn.request("Target.attachToTarget", {"targetId": "a", "flatten": True})
        browser.emit("Page.lifecycleEvent", {"name": "networkIdle"})
        _settle(lambda: any(e.get("method") == "Page.lifecycleEvent" for e in seen))


def test_replies_and_events_never_splice_on_the_shared_socket(served):
    """Two daemon threads write to one socket — the client handler and the CDP reader.
    Unguarded, they interleave two JSON objects onto one line and every frame after is
    garbage. 40 concurrent calls under a steady event stream is the shape that catches it.
    """
    browser, _ = served
    stop = threading.Event()

    def noisy():
        while not stop.is_set():
            browser.emit("Page.lifecycleEvent", {"name": "tick"})
            time.sleep(0.001)

    threading.Thread(target=noisy, daemon=True).start()
    got, lock = [], threading.Lock()
    with RemoteConnection("clienttest") as conn:
        def ask(i):
            r = conn.request("Runtime.evaluate", {"expression": f"e{i}"})
            with lock:
                got.append(r["result"]["echo"])
        threads = [threading.Thread(target=ask, args=(i,)) for i in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
    stop.set()
    assert sorted(got) == sorted(f"e{i}" for i in range(40))


def test_concurrent_client_requests_serialize_stream_writes(served):
    """The client has one stream socket. Force sendall() to stay in progress long enough
    for sibling threads to collide; a connection-level write lock must keep that from ever
    reaching the transport."""
    _, _ = served

    class RejectConcurrentWrites:
        def __init__(self, sock):
            self.sock = sock
            self.lock = threading.Lock()
            self.active = False

        def sendall(self, data):
            with self.lock:
                if self.active:
                    raise OSError("concurrent stream write")
                self.active = True
            try:
                time.sleep(0.02)
                self.sock.sendall(data)
            finally:
                with self.lock:
                    self.active = False

        def __getattr__(self, name):
            return getattr(self.sock, name)

    got, errors = [], []
    start = threading.Barrier(8, timeout=5)
    with RemoteConnection("clienttest") as conn:
        conn._sock = RejectConcurrentWrites(conn._sock)

        def ask(i):
            start.wait()
            try:
                got.append(conn.request("Runtime.evaluate", {"expression": f"e{i}"}))
            except Exception as error:  # noqa: BLE001 — the assertion reports every failure
                errors.append(error)

        threads = [threading.Thread(target=ask, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

    assert not errors
    assert len(got) == 8


def test_a_typed_failure_survives_the_ipc_hop(served):
    _, _ = served
    with RemoteConnection("clienttest") as conn, pytest.raises(HarnessError) as e:
        conn.request("Runtime.evaluate", {"expression": "x"}, session_id="ghost")
    assert e.value.cls is Class.SESSION_STALE          # a class, not a sentence


def test_requesting_on_a_closed_client_fails_fast(served):
    conn = RemoteConnection("clienttest")
    conn.close()
    with pytest.raises(BrowserDisconnected):
        conn.request("Runtime.evaluate")


def test_ensure_daemon_returns_the_pong_when_one_is_already_up(served):
    assert ensure_daemon("clienttest")["pong"] is True


def test_ensure_daemon_reports_a_typed_failure_rather_than_hanging(runtime):
    with pytest.raises(BrowserDisconnected) as e:
        ensure_daemon("nosuchdaemon", timeout=1.0)
    assert e.value.observed["daemon"] == "nosuchdaemon"


# --- the client-side registry ------------------------------------------------

def test_the_registry_caches_so_a_hot_loop_is_not_an_ipc_round_trip(served):
    browser, _ = served
    with RemoteConnection("clienttest") as conn:
        reg = RemoteRegistry(conn)
        first = reg.ready_session("a")
        before = browser.attach_count["a"]
        for _ in range(10):
            assert reg.ensure_live("a").session_id == first.session_id
        assert browser.attach_count["a"] == before      # one attach, not eleven


def test_a_destroyed_target_drops_the_cached_session(served):
    """Otherwise the client keeps issuing commands against a session the browser already
    dropped, and the daemon's registry — the real one — never gets consulted again."""
    browser, _ = served
    with RemoteConnection("clienttest") as conn:
        reg = RemoteRegistry(conn)
        reg.ready_session("a")
        browser.destroy("a")
        _settle(lambda: "a" not in reg._cache)
        with pytest.raises(HarnessError):
            reg.ensure_live("a")


def test_attaching_to_a_target_that_never_existed_is_typed(served):
    with RemoteConnection("clienttest") as conn:
        reg = RemoteRegistry(conn)
        with pytest.raises(HarnessError) as e:
            reg.ready_session("nope")
        assert e.value.cls is Class.TARGET_GONE


def _settle(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("event was never applied")
