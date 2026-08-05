"""Connection tests: id-multiplexing, typed errors, and the single prose boundary."""
import threading
import time

import pytest

from harness.connect.cdp import Connection, classify
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


def test_timeout_is_typed_and_names_the_method(conn):
    with pytest.raises(Timeout) as e:
        conn.request("Runtime.evaluate", timeout=0.0)
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
