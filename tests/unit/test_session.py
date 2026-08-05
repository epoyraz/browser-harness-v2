"""Session registry tests. Every one of these is a v1 bug that shipped.

The headline assertions are `test_one_function_enables_domains` (v1 had three attach paths,
one of which enabled domains) and the lifecycle tests (v1 subscribed to no target events at
all, so it could only learn a session had died by failing a command and reading the prose).
"""
import re
import threading
from pathlib import Path

import pytest

from harness.connect.cdp import Connection
from harness.connect.session import DEFAULT_DOMAINS, SessionRegistry, State
from harness.core.outcome import Class, HarnessError
from tests.fake_browser import FakeBrowser


@pytest.fixture
def wired():
    browser = FakeBrowser("a", "b", "c")
    conn = Connection(browser).start()
    registry = SessionRegistry(conn)
    yield browser, conn, registry
    conn.close()


# --- TODO 9: exactly one producer -------------------------------------------

def test_ready_session_attaches_and_enables_every_default_domain(wired):
    browser, _, registry = wired
    session = registry.ready_session("a")
    assert session.live and session.session_id
    assert browser.domains_for("a") == list(DEFAULT_DOMAINS)


def test_switching_tabs_enables_domains_on_the_new_tab_too(wired):
    """v1's exact bug: `switch_tab` attached without enabling, so Network events stopped
    arriving on every tab after the first — silently, with no error anywhere."""
    browser, _, registry = wired
    registry.ready_session("a")
    registry.ready_session("b")
    assert browser.domains_for("b") == list(DEFAULT_DOMAINS)


def test_ready_session_is_idempotent_and_costs_nothing_the_second_time(wired):
    browser, _, registry = wired
    first = registry.ready_session("a")
    before = len(browser.calls)
    second = registry.ready_session("a")
    assert first.session_id == second.session_id
    assert len(browser.calls) == before          # no round trip for a live session


def test_concurrent_attach_to_one_target_produces_one_session(wired):
    """v1's `js()` attached a fresh session per call and never detached — a leak per call."""
    browser, _, registry = wired
    seen, barrier = [], threading.Barrier(8)

    def attach():
        barrier.wait()
        seen.append(registry.ready_session("a").session_id)

    threads = [threading.Thread(target=attach) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(set(seen)) == 1
    assert browser.attach_count["a"] == 1


def test_one_function_enables_domains(wired):
    """TODO 9's done-when, asserted by grep over the shipped tree.

    Structural rather than behavioural on purpose: the invariant is that no *future* path
    can attach without enabling, and only a whole-tree check can say that.
    """
    root = Path(__file__).resolve().parents[2] / "harness"
    callers = set()
    for path in root.rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if re.search(r"""["']\{?\w*\}?\.?\w*\.enable["']|f["']\{domain\}\.enable""", line):
                callers.add(path.name)
    assert callers == {"session.py"}, f"domains enabled outside the one producer: {callers}"


# --- TODO 10: typed states, learned from events ------------------------------

def test_a_destroyed_target_is_known_dead_without_a_probe(wired):
    browser, _, registry = wired
    registry.ready_session("a")
    browser.destroy("a")
    _settle(lambda: not registry.live_targets)
    with pytest.raises(HarnessError) as e:
        registry.ensure_live("a")
    assert e.value.cls in (Class.TARGET_GONE, Class.SESSION_STALE)


def test_a_crash_is_renderer_unresponsive_not_a_mystery(wired):
    browser, _, registry = wired
    registry.ready_session("a")
    browser.crash("a")
    _settle(lambda: "a" not in registry.live_targets)
    with pytest.raises(HarnessError) as e:
        registry.ensure_live("a")
    assert e.value.cls is Class.RENDERER_UNRESPONSIVE
    assert e.value.observed["state"] == State.RENDERER_UNRESPONSIVE.value


def test_attaching_to_a_target_that_never_existed_is_target_gone(wired):
    _, _, registry = wired
    with pytest.raises(HarnessError) as e:
        registry.ready_session("nope")
    assert e.value.cls is Class.TARGET_GONE
    assert e.value.observed["target_id"] == "nope"


def test_losing_the_browser_kills_every_session_at_once(wired):
    _, _, registry = wired
    registry.ready_session("a")
    registry.ready_session("b")
    registry.disconnected("websocket closed")
    for target in ("a", "b"):
        with pytest.raises(HarnessError) as e:
            registry.ensure_live(target)
        assert e.value.cls is Class.BROWSER_DISCONNECTED


def test_ensure_live_attaches_lazily_when_it_has_never_seen_the_target(wired):
    _, _, registry = wired
    assert registry.ensure_live("c").live          # same code path, not a second one


def test_a_dead_session_can_be_reattached_after_forget(wired):
    browser, _, registry = wired
    registry.ready_session("a")
    browser.destroy("a")
    _settle(lambda: "a" not in registry.live_targets)
    browser.targets["a"] = {"targetId": "a", "type": "page", "url": "https://a.test/"}
    registry.forget("a")
    assert registry.ready_session("a").live


# --- D1: never pooled, never shared -----------------------------------------

def test_two_targets_get_two_distinct_sessions(wired):
    _, _, registry = wired
    assert registry.ready_session("a").session_id != registry.ready_session("b").session_id


def test_a_command_lands_on_the_target_it_named(wired):
    """#375: no shared 'current tab' cursor, so two clients cannot steal each other's tab."""
    _, conn, registry = wired
    for target in ("a", "b", "c"):
        session = registry.ready_session(target)
        landed = conn.request("Runtime.evaluate", {"expression": "x"},
                              session_id=session.session_id)
        assert landed["result"]["value"] == target


def _settle(predicate, timeout: float = 2.0) -> None:
    """Wait for the reader thread to apply an event. Events are asynchronous by nature."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("event was never applied")
