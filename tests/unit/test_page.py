"""Tab primitive tests against the fake. The measured done-whens (overshoot, snapshot
latency, screenshot pixels) live in tests/live/check.py against real Chrome."""
import json
import time

import pytest

from harness.connect.cdp import Connection
from harness.connect.session import SessionRegistry
from harness.core.outcome import (
    CdpError,
    Class,
    ElementGone,
    JsException,
    NavigationFailed,
    NotSerializable,
    SideEffectRefused,
    Timeout,
    ValueRejected,
)
from harness.ops.page import ANNOTATE_JS, WORLD, Tab
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
    assert r == {"requested": "https://a.test/", "landed": "https://a.test/landed",
                 "lifecycle": "load"}


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


def test_goto_settles_when_one_stalled_subresource_holds_load_forever(wired):
    """A single accepted-but-never-answered image, stylesheet or iframe is enough to hold
    `load` open indefinitely while the document is parsed and the form is fillable.

    Measured against real Chrome on all three subresource kinds: DOMContentLoaded fires,
    paint completes, five controls are present and writable — and `load` never arrives.
    The old code waited out the whole timeout and then raised, discarding the page. Across
    three live runs that was 505 seconds, every second of it spent on pages the caller
    went on to fill successfully after catching the exception.
    """
    browser, _, _ = wired
    tab = _tab(wired)
    browser.lifecycle_names = ["DOMContentLoaded", "networkAlmostIdle"]   # no `load`
    browser.eval_hook = lambda e: "https://a.test/" if "location" in e else None
    started = time.monotonic()
    r = tab.goto("https://a.test/", timeout=5.0)
    assert r["lifecycle"] == "settled"
    assert time.monotonic() - started < 1.0          # not the 5s timeout


def test_goto_still_prefers_load_when_the_page_is_healthy(wired):
    """The safety of settling early rests entirely on event ORDER: Chrome emits
    DOMContentLoaded -> load -> networkAlmostIdle, so on a healthy page `load` wins the
    race and the fallback never fires. If that stopped being true, goto would start
    returning before documents finished loading — so assert the order explicitly."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.lifecycle_names = ["DOMContentLoaded", "load", "networkAlmostIdle"]
    browser.eval_hook = lambda e: "https://a.test/" if "location" in e else None
    assert tab.goto("https://a.test/", timeout=5.0)["lifecycle"] == "load"


def test_goto_still_raises_when_the_document_is_genuinely_empty(wired):
    """Settling early must not become "never fail". A page that produced no lifecycle
    event AND has no content is a real failure and still raises."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.lifecycle_names = []
    browser.eval_hook = lambda e: (["loading", 0, 0] if "readyState" in e else None)
    with pytest.raises(Timeout):
        tab.goto("https://a.test/", timeout=0.3)


def test_goto_returns_a_usable_document_even_with_no_lifecycle_event(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.lifecycle_names = []
    browser.eval_hook = lambda e: (["interactive", 7, 900] if "readyState" in e
                                   else "https://a.test/" if "location" in e else None)
    r = tab.goto("https://a.test/", timeout=0.3)
    assert r["lifecycle"] == "timeout"                # honest about how it got here
    assert r["landed"] == "https://a.test/"


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


def test_frames_does_not_sleep_out_a_fixed_budget_on_a_frameless_page(wired):
    """Most pages have no out-of-process iframes, and finding that out used to cost 1.2s.

    `frames()` ran an optimistic auto-attach pass, slept 0.6s for announcements, then —
    because nothing arrived — ran a second retoggling pass and slept 0.6s again. Measured
    on real Chrome: 1206-1258ms, every time, on a page with zero frames. That fixed cost
    was the whole of `prepare_application`'s p50 (1225ms, p90 1246, max 1351 across 160
    live calls; a spread far too tight to be work).

    Toggling unconditionally makes the second pass unnecessary, and settling on a quiet
    window rather than a fixed sleep makes the first one short.
    """
    tab = _tab(wired)
    started = time.monotonic()
    assert tab.frames() == []
    assert time.monotonic() - started < 0.5


def test_frames_collects_every_announcement_not_just_the_first(wired):
    """The old wait returned on the FIRST announcement and read the buffer immediately, so
    a page with several OOPIFs reported only the ones that had arrived by then. Silent
    under-reporting: the caller saw a short list and no indication it was short."""
    browser, _, _ = wired
    tab = _tab(wired)
    import threading
    for i, delay in enumerate((0.02, 0.06, 0.10)):
        threading.Timer(delay, lambda i=i: browser.emit(
            "Target.attachedToTarget",
            {"targetInfo": {"targetId": f"f{i}", "type": "iframe",
                            "url": f"https://x{i}.test/"}},
            session_id=tab._session_id)).start()
    got = tab.frames()
    assert [f["target_id"] for f in got] == ["f0", "f1", "f2"]


def test_a_click_that_did_nothing_falls_back_to_the_dom(wired):
    """The bug that made every click inside `parallel()` a no-op.

    A compositor click is delivered to a renderer, and the renderer can drop it with no
    error anywhere. Measured on four Recruitee postings: Apply navigates and yields 35
    fields when the tab is in front, and does nothing at all for the three of four tabs
    that are not — with the delta honestly reporting no navigation and zero mutations.
    `parallel()` puts every worker but one in that state, which is why Recruitee scored
    0/4 in all five historical runs while the schema kept reporting ~92 controls waiting
    behind that button.
    """
    browser, _, _ = wired
    tab = _tab(wired)
    state = {"clicked": False}

    def hook(e):
        if "elementFromPoint" in e:
            state["clicked"] = True
            return True
        if "__bh.refs" in e:                  # checked first: it also mentions location
            return [10.0, 20.0, "https://a.test/", 0, None]
        if "location.href" in e:
            return ["https://a.test/after", 0] if state["clicked"] \
                else ["https://a.test/", 0]
        return None

    browser.eval_hook = hook
    d = tab.click_ref("e1", settle=0.01)
    assert state["clicked"] is True
    assert d["modality"] == "dom"
    assert d["navigated"] is True


def test_a_click_that_did_something_is_never_repeated(wired):
    """The fallback fires only on an observably inert click. A click that navigated,
    mutated the DOM, opened a dialog or spawned a target did something, and repeating it
    would activate the control twice."""
    browser, _, _ = wired
    tab = _tab(wired)
    fired = {"dom": False}

    def hook(e):
        if "elementFromPoint" in e:
            fired["dom"] = True
            return True
        if "__bh.refs" in e:                  # checked first: it also mentions location
            return [10.0, 20.0, "https://a.test/", 0, None]
        if "location.href" in e:
            return ["https://a.test/", 7]     # 7 mutations: the click plainly landed
        return None

    browser.eval_hook = hook
    d = tab.click_ref("e1", settle=0.01)
    assert fired["dom"] is False
    assert d["modality"] == "compositor"
    assert d["dom_mutations"] == 7


# --- the keyboard twin: delivery-verified keys, DOM synthesis when dropped ----

def test_type_chars_synthesizes_when_the_renderer_dropped_every_key(wired):
    """The counter delta is the evidence: `__bh.keys` unchanged across the dispatch means
    no keydown reached the document — which is what the renderer does, silently, for any
    tab that is not its window's selected tab. `parallel()` puts every worker but at most
    one in that state, so an unverified typed write there typed into nothing."""
    browser, _, _ = wired
    tab = _tab(wired)

    def hook(e):
        if "bh-synth-keys" in e:
            return {"delivered": 0, "synthesized": True}
        if "__bh.keys" in e:
            return 41                          # the pre-dispatch counter reading
        return None

    browser.eval_hook = hook
    out = tab.type_chars("zur", ref="e1")
    keys = [c for c in browser.calls if c.get("method") == "Input.dispatchKeyEvent"]
    assert [k["params"]["type"] for k in keys] == ["keyDown", "keyUp"] * 3
    assert out == {"chars": 3, "modality": "dom", "delivered": 0}


def test_type_chars_never_synthesizes_when_the_keys_arrived(wired):
    """Synthesis after real delivery would type everything twice; the check JS returns
    the delta and the Python side must believe it."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: ({"delivered": 3, "synthesized": False}
                                   if "bh-synth-keys" in e
                                   else 7 if "__bh.keys" in e else None)
    out = tab.type_chars("zur", ref="e1")
    assert out["modality"] == "compositor"
    assert out["delivered"] == 3


def test_press_key_falls_back_through_the_dom_when_undelivered(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: ({"synthesized": True} if "bh-synth-key " in e
                                   else 0 if "__bh.keys" in e else None)
    out = tab.press_key("Escape")
    downs = [c for c in browser.calls if c.get("method") == "Input.dispatchKeyEvent"]
    assert len(downs) == 2                     # the trusted attempt still happened first
    assert out == {"key": "Escape", "modality": "dom"}


def test_scroll_falls_back_through_the_dom_when_no_scroll_event_arrived(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: (
        {"y": 600, "height": 2000, "atBottom": False, "modality": "dom"}
        if "bh-synth-scroll" in e else 0 if "__bh.scrolls" in e else None)
    out = tab.scroll(600)
    assert out["modality"] == "dom"
    assert out["y"] == 600


# --- the wait that answers the question callers were actually asking ----------

def test_wait_for_form_reports_no_form_instead_of_raising(wired):
    """A posting page with no form is an ordinary answer, not an error. `wait_for` could
    only express it by timing out, so every call site wrapped it in `except: pass` and
    threw away the counts that explain why."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: ({"matched": False, "immediate": False, "fields": 0}
                                   if "minFields" in e
                                   else [0, 0, 640] if "offsetParent" in e else None)
    r = tab.wait_for_form(timeout=0.2)
    assert r["ready"] is False
    assert r["controls_in_dom"] == 0 and r["text_len"] == 640


def test_wait_for_form_does_not_resolve_on_a_cookie_banner(wired):
    """The false positive that made `wait_for` useless on late-rendering ATSs: a blanket
    `input, textarea, form` selector matches the consent checkbox in ~10ms and reports
    success while the real form is still 1.5s away. Measured live on the fixture: at that
    moment the page had 0 real fields. The count-based condition excludes furniture, so
    the same page reports not-ready."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: ({"matched": False, "immediate": False, "fields": 0}
                                   if "minFields" in e
                                   else [1, 1, 200] if "offsetParent" in e else None)
    r = tab.wait_for_form(timeout=0.2)
    assert r["ready"] is False               # one furniture control is not a form
    assert r["controls_in_dom"] == 1


def test_wait_for_form_resolves_immediately_when_the_form_is_there(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: ({"matched": True, "immediate": True, "fields": 6}
                                   if "minFields" in e
                                   else [6, 6, 900] if "offsetParent" in e else None)
    r = tab.wait_for_form(timeout=5.0)
    assert r["ready"] is True and r["immediate"] is True
    assert r["waited_ms"] < 500


def test_wait_for_form_rearms_after_the_document_is_replaced(wired):
    """An observer lives in the isolated world, and a world dies with its document — so
    after a navigation the callback can never fire and the wait can only end by timing out.

    Measured on Recruitee, where the apply control navigates to `/c/new`: sequentially the
    form was found in 4ms; under a 10-worker run all four postings burned the full budget
    and then reported the OLD page's counts. Re-arming on the new document is the
    difference between event-driven and event-driven-until-the-first-navigation.
    """
    browser, _, _ = wired
    tab = _tab(wired)
    calls = {"n": 0}

    def hook(e):
        if "minFields" in e:
            calls["n"] += 1
            # first document: no form, and it is about to be replaced.
            # second document (after re-arm): the form is there.
            return {"matched": calls["n"] > 1, "immediate": False, "fields": 0}
        return [6, 6, 900] if "offsetParent" in e else None

    browser.eval_hook = hook
    import threading
    threading.Timer(0.05, lambda: browser.emit(
        "Runtime.executionContextsCleared", {}, session_id=tab._session_id)).start()
    r = tab.wait_for_form(timeout=3.0)
    assert r["ready"] is True
    assert calls["n"] >= 2                    # it re-probed the replacement document
    assert r["waited_ms"] < 1500              # not the 3s timeout


def test_wait_for_form_does_not_spin_on_a_replayed_navigation(wired):
    """`wait_match` re-scans everything buffered since arming, so a navigation already
    handled would match again on the next pass. Without consuming it, the loop spins."""
    browser, _, _ = wired
    tab = _tab(wired)
    probes = {"n": 0}

    def hook(e):
        if "minFields" in e:
            probes["n"] += 1
            return {"matched": False, "immediate": False, "fields": 0}
        return [0, 0, 0] if "offsetParent" in e else None

    browser.eval_hook = hook
    browser.emit("Page.frameNavigated", {}, session_id=tab._session_id)
    time.sleep(0.05)
    r = tab.wait_for_form(timeout=0.6)
    assert r["ready"] is False
    assert probes["n"] <= 4                   # a spin would be hundreds


def test_wait_for_form_rejects_a_nonsense_threshold(wired):
    with pytest.raises(ValueError):
        _tab(wired).wait_for_form(min_fields=0)


def test_application_state_returns_a_real_form_immediately(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: (
        {"matched": True, "immediate": True, "state": "form", "fields": 8,
         "controls": 11, "text_len": 844, "title": "Software Architect",
         "url": "https://jobs.test/application", "ready_state": "complete"}
        if "hasSubmit" in e else None)
    result = tab.wait_for_application_state(timeout=2.0)
    assert result["state"] == "form" and result["immediate"] is True
    assert result["waited_ms"] < 500


def test_application_state_requires_a_quiet_usable_ui(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: (
        {"matched": False, "immediate": False, "state": "usable_ui", "fields": 0,
         "controls": 1, "text_len": 900, "title": "Job", "url": "https://a.test/",
         "ready_state": "complete"}
        if "hasSubmit" in e else None)
    started = time.monotonic()
    result = tab.wait_for_application_state(
        timeout=1.0, usable_stable=0.08, empty_stable=0.2)
    assert result["state"] == "usable_ui" and result["reason"] == "stable"
    assert time.monotonic() - started >= 0.06


def test_application_state_does_not_treat_title_plus_empty_body_as_terminal(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: (
        {"matched": False, "immediate": False, "state": "loading", "fields": 0,
         "controls": 0, "text_len": 0, "title": "Backend Engineer @ Air Apps",
         "url": "https://jobs.test/posting", "ready_state": "complete"}
        if "hasSubmit" in e else None)
    started = time.monotonic()
    result = tab.wait_for_application_state(
        timeout=1.0, usable_stable=0.05, empty_stable=0.12)
    assert result["state"] == "stable_failure" and result["immediate"] is False
    assert time.monotonic() - started >= 0.10


def test_application_state_rejects_invalid_stability_windows(wired):
    with pytest.raises(ValueError):
        _tab(wired).wait_for_application_state(empty_stable=0)


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


def test_a_dialog_that_closes_before_dismissal_does_not_erase_the_click(wired, monkeypatch):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: (
        [10.0, 10.0, "https://a.test/", 0] if "__bh.refs" in e else ["https://a.test/", 0])
    real_cdp = tab.cdp

    def raced(method, *args, **kwargs):
        if method == "Page.handleJavaScriptDialog":
            raise CdpError("No dialog is showing", code=-32602)
        return real_cdp(method, *args, **kwargs)

    monkeypatch.setattr(tab, "cdp", raced)
    browser.emit("Page.javascriptDialogOpening", {"type": "alert", "message": "gone"},
                 session_id=tab._session_id)
    time.sleep(0.05)
    delta = tab.click_ref("e1", settle=0.01)
    assert delta["dialog"] == {"type": "alert", "message": "gone"}


def test_beforeunload_is_accepted_immediately_so_navigation_is_not_blocked(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.emit("Page.javascriptDialogOpening",
                 {"type": "beforeunload", "message": "Leave site?"},
                 session_id=tab._session_id)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        handled = [c for c in browser.calls
                   if c.get("method") == "Page.handleJavaScriptDialog"]
        if handled:
            break
        time.sleep(0.01)
    assert handled and handled[0]["params"]["accept"] is True
    assert tab._dialog is None


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
    runtime = next(c for c in installed if c["params"].get("worldName") == WORLD)
    assert runtime["params"]["worldName"] == WORLD    # survives every navigation


def test_the_dry_run_guard_is_installed_in_the_main_world_before_page_script(wired):
    browser, _, _ = wired
    _tab(wired)
    installed = [c for c in browser.calls
                 if c.get("method") == "Page.addScriptToEvaluateOnNewDocument"]
    safety = next(c for c in installed if "browser-harness.dry-run" in c["params"]["source"])
    assert "worldName" not in safety["params"]
    assert safety["params"]["runImmediately"] is True


def test_submit_ref_is_refused_before_mouse_dispatch(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: (
        [15.0, 25.0, "https://a.test/", 5,
         {"danger": True, "tag": "button", "type": "submit", "action": "/apply"}]
        if "__bh.refs" in e else ["https://a.test/", 5])
    with pytest.raises(SideEffectRefused):
        tab.click_ref("e1")
    assert not any(c.get("method") == "Input.dispatchMouseEvent" for c in browser.calls)


def test_enter_in_a_form_is_refused_before_key_dispatch(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: {"danger": True, "tag": "input", "action": "/apply"}
    with pytest.raises(SideEffectRefused):
        tab.press_key("Enter")
    assert not any(c.get("method") == "Input.dispatchKeyEvent" for c in browser.calls)
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


# --- upload_file: the return must distinguish success from a wrong element ----

def test_uploading_to_a_ref_that_is_not_a_file_input_is_refused(wired, tmp_path):
    """The failure this exists to stop: snapshot() skipped a display:none CV input, so the
    only file ref on the page was an unrelated control. Setting files on it reported
    `attached: []` — byte-identical to the success case, where the page consumes the file
    and clears the input. Silence there cost a real debugging session."""
    browser, _conn, _reg = wired
    t = _tab(wired)
    doc = tmp_path / "cv.pdf"
    doc.write_bytes(b"%PDF-1.4\n")

    def hook(expr):
        if "returnByValue" in expr or "__bh.refs[" in expr and "tagName" not in expr:
            return {"__raw__": {"result": {"type": "object", "objectId": "obj-1"}}}
        return {"tag": "div", "type": None, "name": "dropzone", "accept": ""}
    browser.eval_hook = hook

    with pytest.raises(ElementGone) as e:
        t.upload_file("e7", str(doc))
    assert e.value.observed["tag"] == "div"
    assert not [c for c in browser.calls if c.get("method") == "DOM.setFileInputFiles"]


def test_a_file_the_accept_filter_excludes_is_named_not_silently_empty(wired, tmp_path):
    """`.txt` against an accept of pdf/doc/png attaches nothing and raises nothing. The
    caller has to be told which file the filter dropped, or `attached: []` reads as the
    ordinary consumed-by-the-page case."""
    browser, _conn, _reg = wired
    t = _tab(wired)
    doc = tmp_path / "cv.txt"
    doc.write_text("not a pdf")
    accept = "application/pdf, application/msword, image/png"

    def hook(expr):
        if "tagName" in expr:
            return {"tag": "input", "type": "file", "name": "cv", "accept": accept}
        if "files" in expr:
            return []
        return {"__raw__": {"result": {"type": "object", "objectId": "obj-1"}}}
    browser.eval_hook = hook

    out = t.upload_file("e7", str(doc))
    assert out["attached"] == []
    assert out["accept_rejected"] == ["cv.txt"]
    assert not out.ok and out.cls is Class.VALUE_REJECTED
    assert out.to_json()["observed"]["accept_rejected"] == ["cv.txt"]
    with pytest.raises(ValueRejected) as error:
        out.unwrap()
    assert error.value.observed["accept_rejected"] == ["cv.txt"]


def test_a_consumed_file_is_not_reported_as_an_accept_rejection(wired, tmp_path):
    """The common success shape: the page's change handler moves the file into its own
    state and clears the input. Empty here is normal and must not be blamed on `accept`."""
    browser, _conn, _reg = wired
    t = _tab(wired)
    doc = tmp_path / "cv.pdf"
    doc.write_bytes(b"%PDF-1.4\n")

    def hook(expr):
        if "tagName" in expr:
            return {"tag": "input", "type": "file", "name": "cv",
                    "accept": "application/pdf"}
        if "files" in expr:
            return []
        return {"__raw__": {"result": {"type": "object", "objectId": "obj-1"}}}
    browser.eval_hook = hook

    out = t.upload_file("e7", str(doc))
    assert out["consumed_or_rejected"] is True
    assert "accept_rejected" not in out
    assert out.ok and out.value is out and out.unwrap() is out


def test_a_partially_attached_upload_reports_the_accept_rejection(wired, tmp_path):
    browser, _conn, _reg = wired
    tab = _tab(wired)
    pdf = tmp_path / "cv.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    text = tmp_path / "notes.txt"
    text.write_text("not a pdf")

    def hook(expr):
        if "tagName" in expr:
            return {"tag": "input", "type": "file", "name": "cv", "accept": ".pdf"}
        if "files" in expr:
            return ["cv.pdf"]
        return {"__raw__": {"result": {"type": "object", "objectId": "obj-1"}}}

    browser.eval_hook = hook
    out = tab.upload_file("e7", [str(pdf), str(text)])
    assert out["attached"] == ["cv.pdf"]
    assert out["accept_rejected"] == ["notes.txt"]
    assert not out.ok and out.cls is Class.VALUE_REJECTED


# --- vision: the other half of perception ------------------------------------

def test_see_returns_elements_and_a_frame_that_share_one_index(wired, tmp_path):
    """The point of the pairing: look at the picture, act on the ref. A model reading
    coordinates off an unannotated image estimates them; here every box carries its ref."""
    browser, _, _ = wired
    tab = _tab(wired)
    els = [{"ref": "e1", "tag": "button", "name": "Apply", "x": 50, "y": 20,
            "w": 80, "h": 24}]
    browser.eval_hook = lambda e: els if "querySelectorAll" in e else 1
    out = tab.see(tmp_path / "s.jpg")
    assert out["elements"] == els and out["marked"] == 1
    assert (tmp_path / "s.jpg").exists()


def test_the_marks_are_removed_even_when_the_capture_fails(wired):
    """A leftover overlay is `position:fixed` at the top z-index — it would change what
    every later click lands on, so removal cannot be conditional on success."""
    browser, _, _ = wired
    tab = _tab(wired)
    removals = []

    def hook(expr):
        if "querySelectorAll" in expr:
            return [{"ref": "e1", "tag": "a", "name": "x", "x": 5, "y": 5, "w": 10, "h": 10}]
        if "__bh_marks" in expr and "remove" in expr:
            removals.append(expr)
        return 1
    browser.eval_hook = hook
    browser.hang_methods = {"Page.captureScreenshot"}      # a capture that never answers
    with pytest.raises(Timeout):
        tab.see(timeout=0.3)
    assert removals, "the overlay outlived a failed capture"


def test_a_hidden_control_gets_no_mark(wired, tmp_path):
    """Measured on the Select2 fixture: the real 250-option <select> is clipped to 1x1, so
    it has no box to draw. Vision cannot see the control that actually submits — which is
    precisely why `see()` returns the schema-visible elements alongside the image."""
    browser, _, _ = wired
    tab = _tab(wired)
    els = [{"ref": "e1", "tag": "select", "name": "country", "x": 0, "y": 0,
            "w": 0, "h": 0, "hidden_control": True},
           {"ref": "e2", "tag": "input", "name": "city", "x": 40, "y": 60,
            "w": 120, "h": 24}]
    drawn = {}

    def hook(expr):
        if "querySelectorAll" in expr:
            return els
        if "__bh_marks" in expr and "appendChild" in expr:
            drawn["js"] = expr
            return 1
        return 1
    browser.eval_hook = hook
    out = tab.see(tmp_path / "s.jpg")
    assert len(out["elements"]) == 2                 # both reachable structurally
    assert '"w": 0' in drawn["js"]                   # the zero-box one is passed through
    # ...and ANNOTATE_JS skips it: `if (!e.w || !e.h) continue`
    assert "if (!e.w || !e.h) continue" in ANNOTATE_JS


def test_marks_can_be_turned_off_for_a_human_frame(wired, tmp_path):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: [] if "querySelectorAll" in e else 1
    out = tab.see(tmp_path / "s.jpg", marks=False)
    assert out["marked"] == 0


def test_a_hidden_file_input_is_reachable_by_css_selector(wired, tmp_path):
    """The bug this closes: refs come from snapshot(), snapshot only registers elements
    with a box, and a file input almost never has one — the standard pattern is a
    display:none input behind a styled dropzone. So upload_file was unreachable for
    exactly the case it exists to serve, and driving joblens' own CV field meant dropping
    to raw DOM.querySelector + DOM.setFileInputFiles by hand."""
    browser, _conn, _reg = wired
    t = _tab(wired)
    doc = tmp_path / "cv.pdf"
    doc.write_bytes(b"%PDF-1.4\n")
    seen: list[str] = []

    def hook(expr):
        seen.append(expr)
        if "tagName" in expr:
            return {"tag": "input", "type": "file", "name": "cv", "accept": ".pdf"}
        if "files" in expr:
            return ["cv.pdf"]
        return {"__raw__": {"result": {"type": "object", "objectId": "obj-1"}}}
    browser.eval_hook = hook

    out = t.upload_file("input[type=file]", str(doc))
    assert out["attached"] == ["cv.pdf"]
    assert json.loads(json.dumps(out)) == dict(out)
    assert out.ok and out.to_json()["observed"]["attached"] == ["cv.pdf"]
    # The registry is still consulted first, so a real ref keeps winning over a selector
    # that happens to look like one.
    assert any("__bh.refs[" in e and "querySelector" in e for e in seen)


def test_an_unresolvable_ref_says_both_things_it_tried(wired, tmp_path):
    browser, _conn, _reg = wired
    t = _tab(wired)
    doc = tmp_path / "cv.pdf"
    doc.write_bytes(b"%PDF-1.4\n")
    browser.eval_hook = lambda expr: {"__raw__": {"result": {"type": "undefined"}}}

    with pytest.raises(ElementGone) as e:
        t.upload_file("nope", str(doc))
    assert "registered ref" in str(e.value) and "CSS selector" in str(e.value)
