"""Tab primitive tests against the fake. The measured done-whens (overshoot, snapshot
latency, screenshot pixels) live in tests/live/check.py against real Chrome."""
import time

import pytest

from harness.connect.cdp import Connection
from harness.connect.session import SessionRegistry
from harness.core.outcome import (
    ElementGone,
    JsException,
    NavigationFailed,
    NotSerializable,
    Timeout,
)
from harness.ops.page import WORLD, Tab
from tests.fake_browser import FakeBrowser


@pytest.fixture
def wired():
    browser = FakeBrowser("a", "b")
    conn = Connection(browser).start()
    registry = SessionRegistry(conn)
    yield browser, conn, registry
    conn.close()


def _tab(wired, **kw):
    _browser, conn, registry = wired
    return Tab(conn, registry, "a", **kw)


# --- item 15: js() -----------------------------------------------------------

def test_js_sends_replmode_and_returnbyvalue(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: 42
    assert tab.js("6*7") == 42
    call = [c for c in browser.calls if c.get("method") == "Runtime.evaluate"][-1]
    assert call["params"]["replMode"] is True          # top-level await works (D14)
    assert call["params"]["returnByValue"] is True


def test_js_exception_is_typed_with_its_description(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: {"__raw__": {"result": {"type": "undefined"},
        "exceptionDetails": {"text": "Uncaught", "lineNumber": 3,
                             "exception": {"description": "ReferenceError: x is not defined\n  at <anon>"}}}}
    with pytest.raises(JsException) as e:
        tab.js("x")
    assert "ReferenceError" in e.value.args[0]
    assert e.value.observed["line"] == 3


def test_an_unserializable_result_raises_instead_of_silent_none(wired):
    """v1 returned None for a DOM-node result, silently — indistinguishable from the page
    genuinely returning null."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: {"__raw__": {"result": {
        "type": "object", "subtype": "node", "description": "body"}}}
    with pytest.raises(NotSerializable):
        tab.js("document.body")


def test_undefined_is_a_legitimate_none(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: {"__raw__": {"result": {"type": "undefined"}}}
    assert tab.js("void 0") is None


# --- item 16 + 19: goto and event-driven waits -------------------------------

def test_goto_returns_requested_and_landed(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: "https://a.test/landed" if "location" in e else None
    r = tab.goto("https://a.test/")
    assert r == {"requested": "https://a.test/", "landed": "https://a.test/landed"}


def test_goto_errortext_is_navigation_failed_with_evidence(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.navigate_error = "net::ERR_CONNECTION_REFUSED"
    with pytest.raises(NavigationFailed) as e:
        tab.goto("https://dead.test/")
    assert "ERR_CONNECTION_REFUSED" in e.value.args[0]
    assert e.value.observed["requested"] == "https://dead.test/"


def test_landing_on_chrome_error_is_navigation_failed(wired):
    """The 404 that v1 reported as a title."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: ("chrome-error://chromewebdata/" if "location" in e
                                   else None)
    with pytest.raises(NavigationFailed) as e:
        tab.goto("https://x.test/careers")
    assert e.value.observed["landed"].startswith("chrome-error://")
    assert e.value.observed["requested"] == "https://x.test/careers"


def test_goto_cannot_miss_a_load_that_races_the_navigate_reply(wired):
    """The fake emits the lifecycle event BEFORE the navigate reply, as Chrome can. The
    waiter is armed before navigate, so the buffered event still satisfies the wait."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: "https://a.test/" if "location" in e else None
    assert tab.goto("https://a.test/", timeout=2.0)["landed"]


def test_wait_lifecycle_wakes_on_the_event_not_on_a_poll(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    import threading
    threading.Timer(0.1, lambda: browser.emit(
        "Page.lifecycleEvent", {"name": "networkIdle"},
        session_id=tab._session_id)).start()
    started = time.monotonic()
    tab.wait_lifecycle("networkIdle", timeout=5.0)
    elapsed = time.monotonic() - started
    assert 0.05 < elapsed < 0.5                      # woke on the event, not the timeout


def test_wait_lifecycle_times_out_typed(wired):
    tab = _tab(wired)
    with pytest.raises(Timeout):
        tab.wait_lifecycle("networkIdle", timeout=0.1)


# --- items 17 + 20: snapshot, refs, deltas ------------------------------------

def test_snapshot_returns_the_elements_the_page_reported(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    els = [{"ref": "e1", "tag": "button", "name": "Apply", "x": 10, "y": 20, "w": 80, "h": 24}]
    browser.eval_hook = lambda e: els if "querySelectorAll" in e else None
    assert tab.snapshot() == els


def test_click_ref_dispatches_at_the_elements_center(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: (
        [15.0, 25.0, "https://a.test/", 5] if "__bh.refs" in e
        else ["https://a.test/", 9])
    delta = tab.click_ref("e1", settle=0.05)
    clicks = [c for c in browser.calls if c.get("method") == "Input.dispatchMouseEvent"]
    assert [c["params"]["type"] for c in clicks] == ["mousePressed", "mouseReleased"]
    assert clicks[0]["params"]["x"] == 15.0 and clicks[0]["params"]["y"] == 25.0
    assert delta["dom_mutations"] == 4               # 9 - 5, same document
    assert delta["navigated"] is False


def test_click_ref_on_a_vanished_element_is_element_gone(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: None
    with pytest.raises(ElementGone) as e:
        tab.click_ref("e9")
    assert e.value.observed["ref"] == "e9"


def test_a_click_that_navigates_reports_it_and_voids_the_dom_delta(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: (
        [10.0, 10.0, "https://a.test/", 3] if "__bh.refs" in e
        else ["https://a.test/next", 0])
    delta = tab.click_ref("e1", settle=0.05)
    assert delta["navigated"] is True
    assert delta["dom_mutations"] is None            # a new document restarts the counter


def test_a_click_that_opens_a_new_tab_reports_the_target(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: (
        [10.0, 10.0, "https://a.test/", 0] if "__bh.refs" in e else ["https://a.test/", 0])
    real_send = browser.send

    def send_and_open(msg):
        real_send(msg)
        if msg.get("method") == "Input.dispatchMouseEvent" \
                and msg["params"]["type"] == "mouseReleased":
            browser.emit("Target.targetCreated",
                         {"targetInfo": {"type": "page", "targetId": "popup1"}})
    browser.send = send_and_open
    delta = tab.click_ref("e1", settle=0.3)
    assert delta["new_targets"] == ["popup1"]


def test_the_dialog_dance_a_blocked_dispatch_is_a_click_that_opened_a_dialog(wired):
    """`Input.dispatchMouseEvent` does not ACK while the click handler's dialog is up.
    That must be reported as a successful click with a dialog, not as a hang."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: (
        [10.0, 10.0, "https://a.test/", 0] if "__bh.refs" in e else ["https://a.test/", 0])
    browser.hang_methods = {"Input.dispatchMouseEvent"}
    real_send = browser.send

    def send_and_dialog(msg):
        real_send(msg)
        if msg.get("method") == "Input.dispatchMouseEvent":
            browser.emit("Page.javascriptDialogOpening",
                         {"type": "confirm", "message": "Sure?"},
                         session_id=tab._session_id)
    browser.send = send_and_dialog
    delta = tab.click_ref("e1", settle=0.05)
    assert delta["dialog"] == {"type": "confirm", "message": "Sure?"}
    handled = [c for c in browser.calls if c.get("method") == "Page.handleJavaScriptDialog"]
    assert handled and handled[0]["params"]["accept"] is False   # dismissed by default


def test_a_genuinely_hung_dispatch_still_raises(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: (
        [10.0, 10.0, "https://a.test/", 0] if "__bh.refs" in e else ["https://a.test/", 0])
    browser.hang_methods = {"Input.dispatchMouseEvent"}          # hangs, but no dialog
    with pytest.raises(Timeout):
        tab.click_ref("e1", settle=0.05, timeout=1.0)


# --- item 18: injected runtime ------------------------------------------------

def test_the_runtime_is_installed_for_every_future_document(wired):
    browser, _, _ = wired
    _tab(wired)
    installed = [c for c in browser.calls
                 if c.get("method") == "Page.addScriptToEvaluateOnNewDocument"]
    assert installed and "__bh" in installed[0]["params"]["source"]


# --- item 21: screenshot ------------------------------------------------------

def test_screenshot_scale_is_the_inverse_of_dpr(wired, tmp_path):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: 2 if "devicePixelRatio" in e else None
    out = tab.capture_screenshot(tmp_path / "shot.jpeg")
    call = next(c for c in browser.calls if c.get("method") == "Page.captureScreenshot")
    assert call["params"]["clip"]["scale"] == 0.5    # dpr 2 → CSS pixels out
    assert call["params"]["format"] == "jpeg" and call["params"]["quality"] == 70
    assert (tmp_path / "shot.jpeg").read_bytes() == b"fake-image-bytes"
    assert out["css_viewport"] == [1200, 800]


def test_max_dim_lowers_the_scale_instead_of_resizing_after(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: 2 if "devicePixelRatio" in e else None
    tab.capture_screenshot(max_dim=600)              # css 1200 wide → scale 600/2400
    call = [c for c in browser.calls if c.get("method") == "Page.captureScreenshot"][-1]
    assert call["params"]["clip"]["scale"] == 0.25


def test_png_when_the_path_says_so(wired, tmp_path):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: 1 if "devicePixelRatio" in e else None
    tab.capture_screenshot(tmp_path / "s.png")
    call = [c for c in browser.calls if c.get("method") == "Page.captureScreenshot"][-1]
    assert call["params"]["format"] == "png" and "quality" not in call["params"]


# --- isolation ----------------------------------------------------------------

def test_events_from_another_tabs_session_are_ignored(wired):
    browser, _conn, registry = wired
    tab = _tab(wired)
    other = registry.ready_session("b").session_id
    browser.emit("Page.javascriptDialogOpening", {"type": "alert", "message": "not mine"},
                 session_id=other)
    time.sleep(0.1)
    assert tab._dialog is None


# --- the machinery runs off-window (2026-08-05 detectability finding) ---------

def test_the_runtime_is_installed_into_an_isolated_world(wired):
    """A page could read `Object.getOwnPropertyNames(window)` and find a stray `__bh`,
    which announces the harness for no benefit. The isolated world shares the DOM and
    has its own global object, so page script cannot see the registry at all."""
    browser, _, _ = wired
    _tab(wired)
    installed = [c for c in browser.calls
                 if c.get("method") == "Page.addScriptToEvaluateOnNewDocument"]
    assert installed[0]["params"]["worldName"] == WORLD    # survives every navigation
    assert WORLD in browser.isolated_worlds                # and exists for this document


def test_harness_evaluates_carry_the_world_context_but_js_does_not(wired):
    """The split that matters: our machinery is invisible, the user's escape hatch is not.
    `js()` must land where the page's globals live or it cannot reach them."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: []
    tab.snapshot()
    tab.js("window.someAppState")
    evals = [c for c in browser.calls if c.get("method") == "Runtime.evaluate"]
    snapshot_call = next(c for c in evals if "querySelectorAll" in c["params"]["expression"])
    user_call = next(c for c in evals if c["params"]["expression"] == "window.someAppState")
    assert snapshot_call["params"].get("contextId") == 77
    assert "contextId" not in user_call["params"]


def test_a_dead_world_is_rebuilt_rather_than_failing_the_call(wired):
    """Isolated worlds die with their document, so the id is re-resolved, never cached
    across a navigation."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: []
    tab.snapshot()
    before = len(browser.isolated_worlds)
    browser.emit("Runtime.executionContextsCleared", {}, session_id=tab._session_id)
    deadline = time.monotonic() + 2
    while tab._world_ctx is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert tab._world_ctx is None
    tab.snapshot()
    assert len(browser.isolated_worlds) == before + 1
