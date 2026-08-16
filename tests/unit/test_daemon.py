"""Daemon tests. TODO 7's done-when is `test_two_clients_drive_two_tabs_concurrently`."""
import os
import threading
import time

import pytest

from harness.connect.daemon import Daemon, request
from harness.connect.session import DEFAULT_DOMAINS
from harness.core.outcome import Class, HarnessError
from tests.fake_browser import FakeBrowser


@pytest.fixture
def runtime(monkeypatch):
    """A short runtime dir. pytest's `tmp_path` is ~128 bytes on macOS, which is already
    over the 104-byte AF_UNIX limit the transport exists to respect."""
    d = f"/tmp/bhd{os.getpid()}"
    os.makedirs(d, exist_ok=True)
    monkeypatch.setenv("BH_RUNTIME_DIR", d)
    return d


def _serve(name, browser):
    """A daemon that is actually ready, not merely bound.

    `start()` publishes the IPC endpoint and opens the browser on a background thread, so
    it returns while the handshake is still in flight — deliberately, so a client can ping
    a connecting daemon and be told what it is waiting for. Production honours that:
    `ensure_daemon` waits for `pong["browser"]`, and `handle()` gates on
    `_browser_pending()`. Tests want the settled daemon, and `ping` is the one call that
    reports readiness rather than waiting for it — so without this the ping test failed
    roughly one full-suite run in ten, and only under load.
    """
    daemon = Daemon(name, browser).start()
    thread = threading.Thread(target=daemon.serve_forever, daemon=True)
    thread.start()
    assert daemon._settled.wait(10), "daemon never finished connecting"
    return daemon


def _settle(predicate, timeout: float = 3.0) -> None:
    """Poll instead of sleeping a guessed interval — events are asynchronous by nature."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("event was never applied")


# --- TODO 7's done-when ------------------------------------------------------

def test_two_clients_drive_two_tabs_concurrently(runtime):
    """Two clients, two tabs, one websocket — overlapping, not queued, and never crossed.

    v1 could not do either half: one shared `current_tab` cursor meant two subagents fought
    over the same tab (#375), and requests were answered one at a time.
    """
    browser = FakeBrowser("a", "b")
    daemon = _serve("con", browser)
    for target in ("a", "b"):
        request("con", {"meta": "attach", "target_id": target})

    browser.latency = 0.3          # measure the concurrent commands, not the attach
    landed, lock = [], threading.Lock()

    def client(target):
        reply = request("con", {"method": "Runtime.evaluate",
                                "params": {"expression": target}, "target_id": target})
        with lock:
            landed.append((target, reply["value"]["result"]["value"]))

    started = time.monotonic()
    threads = [threading.Thread(target=client, args=(t,)) for t in ("a", "b") * 3]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    elapsed = time.monotonic() - started
    daemon.stop()

    # every client got its own tab back — nobody was served another client's target
    assert sorted(landed) == [("a", "a")] * 3 + [("b", "b")] * 3
    assert browser.max_in_flight > 1               # genuinely overlapping on one connection
    assert elapsed < 0.9, f"6 concurrent 300ms calls took {elapsed:.2f}s — they serialised"


def test_the_target_is_a_parameter_not_shared_state(runtime):
    """No 'current tab' anywhere, so interleaved clients cannot drift onto each other."""
    daemon = _serve("param", FakeBrowser("a", "b", "c"))
    for target in ("a", "b", "c", "a", "c", "b"):
        reply = request("param", {"method": "Runtime.evaluate", "target_id": target})
        assert reply["value"]["result"]["value"] == target
    daemon.stop()


# --- outcomes on the wire ----------------------------------------------------

def test_a_failure_crosses_the_wire_as_a_class_not_a_string(runtime):
    """v1 sent `{"error": str(e)}`, so clients string-matched Chrome's prose back out."""
    daemon = _serve("fail", FakeBrowser("a"))
    with pytest.raises(HarnessError) as e:
        request("fail", {"method": "Runtime.evaluate", "target_id": "ghost"})
    daemon.stop()
    assert e.value.cls is Class.TARGET_GONE
    assert e.value.observed["target_id"] == "ghost"


def test_a_dead_tab_reports_itself_and_never_substitutes_another(runtime):
    """D10: recovery fails closed. Silently falling back to `pages[0]` is how a harness
    ends up driving the user's own tab (#479)."""
    browser = FakeBrowser("a", "b")
    daemon = _serve("closed", browser)
    request("closed", {"method": "Runtime.evaluate", "target_id": "a"})
    browser.destroy("a")
    _settle(lambda: "a" not in daemon.sessions.live_targets)
    with pytest.raises(HarnessError) as e:
        request("closed", {"method": "Runtime.evaluate", "target_id": "a"})
    daemon.stop()
    assert e.value.cls in (Class.TARGET_GONE, Class.SESSION_STALE)
    assert e.value.observed.get("target_id") == "a"


def test_ping_reports_the_browser_not_just_the_daemon(runtime):
    """A meta-only pong from a daemon whose CDP socket is dead is what v1 needed six PRs
    to stop reporting as healthy."""
    daemon = _serve("ping", FakeBrowser("a"))
    reply = request("ping", {"meta": "ping"})
    assert reply["pong"] is True and reply["browser"] is True
    assert reply["protocol"] >= 1 and reply["version"] == "0.1.0"
    daemon.stop()


def test_a_malformed_request_is_an_outcome_not_a_crash(runtime):
    daemon = _serve("bad", FakeBrowser("a"))
    assert daemon.handle({"params": {}})["class"] == Class.CDP_ERROR.value
    assert daemon.handle({"meta": "nonsense"})["ok"] is False
    daemon.stop()


def test_attach_is_reported_with_its_session(runtime):
    daemon = _serve("att", FakeBrowser("a"))
    reply = request("att", {"meta": "attach", "target_id": "a"})
    assert reply["ok"] and reply["value"]["state"] == "attached"
    daemon.stop()


def test_many_sequential_requests_reuse_one_session(runtime):
    browser = FakeBrowser("a")
    daemon = _serve("reuse", browser)
    for _ in range(10):
        request("reuse", {"method": "Runtime.evaluate", "target_id": "a"})
    daemon.stop()
    assert browser.attach_count["a"] == 1          # not one attach per call, as v1's js() did


def test_target_event_before_adopt_reply_cannot_deadlock_the_reader(runtime):
    """Adoption must not hold a lock needed by a CDP event subscriber while awaiting a
    reply. Chrome can send targetDestroyed before the Target.getTargets response; the one
    reader must process that event before it can deliver the response behind it."""

    class EventBeforeReplyBrowser(FakeBrowser):
        def _work(self, msg):
            if msg.get("method") == "Target.getTargets":
                self.emit("Target.targetDestroyed", {"targetId": "leased"})
            super()._work(msg)

    browser = EventBeforeReplyBrowser("a")
    daemon = _serve("event-before-adopt", browser)
    with daemon._lease_lock:
        daemon._leases["opaque"] = "leased"
        daemon._lease_for_target["leased"] = "opaque"

    # Keep a regression failure short. Before the fix, `_watch_leases` blocked on the
    # lock held by adoption and Target.getTargets could not be delivered before timeout.
    original_request = daemon.conn.request

    def short_request(method, params=None, *, session_id=None, timeout=10.0):
        return original_request(method, params, session_id=session_id,
                                timeout=min(timeout, 0.5))

    daemon.conn.request = short_request
    started = time.monotonic()
    reply = daemon._meta("adopt", {})
    elapsed = time.monotonic() - started
    daemon.stop()

    assert reply["ok"] is True
    assert reply["value"]["target_id"] == "a"
    assert elapsed < 0.5
    assert "leased" not in daemon._lease_for_target


def test_client_closing_during_adoption_cannot_leave_a_stale_reservation(runtime):
    """Moving CDP outside `_adopt_lock` must not let a disconnected peer install a new
    adoption after its connection cleanup already removed the old one."""
    request_started = threading.Event()
    release_reply = threading.Event()

    class DelayedTargetsBrowser(FakeBrowser):
        def _work(self, msg):
            if msg.get("method") == "Target.getTargets":
                request_started.set()
                release_reply.wait(2)
            super()._work(msg)

    class ClosingPeer:
        def __init__(self):
            self.closed = threading.Event()

    daemon = _serve("close-during-adopt", DelayedTargetsBrowser("a"))
    peer = ClosingPeer()
    result = {}
    thread = threading.Thread(
        target=lambda: result.setdefault("reply", daemon._meta("adopt", {}, peer=peer)))
    thread.start()
    assert request_started.wait(2)
    peer.closed.set()                  # connection cleanup won while CDP was in flight
    release_reply.set()
    thread.join(2)
    daemon.stop()

    assert result["reply"]["ok"] is True
    assert peer not in daemon._adoptions


def test_recovery_never_reaches_an_unrelated_tab(runtime):
    """TODO 13's done-when. v1's `attach_first_page()` substituted `pages[0]` when its
    target was gone — which, against a daily-driver Chrome, is someone's real tab."""
    browser = FakeBrowser("mine", "daily_driver")
    daemon = _serve("fc", browser)
    request("fc", {"method": "Runtime.evaluate", "target_id": "mine"})
    browser.destroy("mine")
    _settle(lambda: "mine" not in daemon.sessions.live_targets)
    with pytest.raises(HarnessError) as e:
        request("fc", {"method": "Runtime.evaluate", "target_id": "mine"})
    daemon.stop()
    assert e.value.observed.get("target_id") == "mine"     # the error names the dead target
    assert browser.attach_count["daily_driver"] == 0       # and nothing touched the other tab


def test_raw_attach_is_routed_through_the_one_producer(runtime):
    """The escape hatch must not be a second way to make a session.

    v1's `js()` issued `Target.attachToTarget` directly on every call, so an iframe-heavy
    page leaked an un-domained session per call. Here the raw method is an alias.
    """
    browser = FakeBrowser("a")
    daemon = _serve("raw", browser)
    replies = [request("raw", {"method": "Target.attachToTarget",
                               "params": {"targetId": "a", "flatten": True}})
               for _ in range(5)]
    daemon.stop()
    assert browser.attach_count["a"] == 1                       # not five
    assert len({r["value"]["sessionId"] for r in replies}) == 1  # CDP's own reply shape
    assert browser.domains_for("a") == list(DEFAULT_DOMAINS)     # and it is domain-enabled


def test_raw_attach_to_a_dead_target_is_typed_too(runtime):
    daemon = _serve("rawdead", FakeBrowser("a"))
    with pytest.raises(HarnessError) as e:
        request("rawdead", {"method": "Target.attachToTarget", "params": {"targetId": "zz"}})
    daemon.stop()
    assert e.value.cls is Class.TARGET_GONE


def test_stopping_removes_the_socket(runtime):
    from harness.core import ipc
    daemon = _serve("gone", FakeBrowser("a"))
    endpoint = ipc.port_path("gone") if ipc.IS_WINDOWS else ipc.sock_path("gone")
    assert endpoint.exists()
    daemon.stop()
    assert not endpoint.exists()


# --- the endpoint is published before the browser handshake -------------------

def test_the_endpoint_is_published_before_the_browser_handshake(runtime):
    """Chrome shows an "Allow remote debugging" prompt per websocket and blocks the
    handshake until it is answered. Publishing the endpoint only after that click made a
    waiting client see 30s of silence — identical to a daemon that never started, and with
    the spawned daemon's stderr going to DEVNULL there was nothing to read either."""
    import threading as _t

    from harness.core import ipc
    from harness.core.outcome import Class

    gate = _t.Event()

    def blocked_handshake():
        gate.wait(10)                     # stands in for the unanswered consent prompt
        return FakeBrowser("a")

    daemon = Daemon("pending", blocked_handshake)
    daemon.start()
    try:
        _t.Thread(target=daemon.serve_forever, daemon=True).start()

        # reachable while the handshake is still blocked
        pong = ipc.ping("pending", timeout=3.0)
        assert pong is not None and pong["pong"] is True
        assert pong["browser"] is False            # not ready, and says so
        assert pong["connecting"] is True
        assert "prompt" in pong["reason"] or "handshake" in pong["reason"]

        # a real request is refused with a class, not left hanging forever
        reply = daemon.handle({"method": "Runtime.evaluate", "timeout": 0.2})
        assert reply["class"] == Class.BROWSER_DISCONNECTED.value

        gate.set()                                 # "user clicks Allow"
        for _ in range(100):
            pong = ipc.ping("pending", timeout=3.0)
            if pong and pong["browser"]:
                break
            time.sleep(0.05)
        assert pong["browser"] is True
        assert "reason" not in pong
    finally:
        gate.set()
        daemon.stop()


def test_a_handshake_that_fails_reports_why_instead_of_going_quiet(runtime):
    from harness.core import ipc

    def refused():
        raise OSError("connection refused by the browser")

    daemon = Daemon("refused", refused)
    daemon.start()
    try:
        threading.Thread(target=daemon.serve_forever, daemon=True).start()
        for _ in range(100):
            pong = ipc.ping("refused", timeout=3.0)
            if pong and not pong.get("connecting"):
                break
            time.sleep(0.05)
        assert pong["browser"] is False
        assert "connection refused" in pong["reason"]
    finally:
        daemon.stop()
