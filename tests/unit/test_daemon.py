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
    daemon = Daemon(name, browser).start()
    thread = threading.Thread(target=daemon.serve_forever, daemon=True)
    thread.start()
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
    assert ipc.sock_path("gone").exists()
    daemon.stop()
    assert not ipc.sock_path("gone").exists()
