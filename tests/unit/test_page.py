"""Tab primitive tests against the fake. The measured done-whens (overshoot, snapshot
latency, screenshot pixels) live in tests/live/check.py against real Chrome."""
import json
import threading
import time

import pytest

from harness.connect.cdp import Connection
from harness.connect.session import SessionRegistry, State
from harness.core.journal import Journal
from harness.core.outcome import (
    CdpError,
    Class,
    ElementGone,
    HarnessError,
    JsException,
    NavigationFailed,
    NotSerializable,
    SideEffectRefused,
    Timeout,
    ValueRejected,
)
from harness.ops.page import ANNOTATE_JS, RUNTIME_JS, SNAPSHOT_JS, WORLD, Tab
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


def _click_hook(*, activates: bool = False):
    """eval_hook for a click on ref `e1` that changes nothing observable — same URL, no
    mutations, no control-state change — which is what makes a click "inert" and puts the
    DOM-activation retry in play. `activates` decides whether that retry claims it clicked
    something, so the delta's `modality` reports whether the retry ran at all."""
    def hook(expression):
        if "getBoundingClientRect" in expression:
            return [10.0, 10.0, "https://a.test/", 0]
        if "pointerdown" in expression:
            return activates
        return ["https://a.test/", 0, None]
    return hook


def _reader_caught_up(tab):
    """Barrier for events emitted from the test thread.

    They are already sitting in the fake's queue and a reply can only be pushed behind
    them, so one completed round trip proves the single reader thread has dispatched every
    one of them. That is how a test asserts an event did NOT have an effect without
    sleeping on a thread that has no other way to say it is done.
    """
    tab.cdp("Page.getLayoutMetrics")


def _open_on_release(browser, info):
    """Announce a page target the instant the click lands, as Chrome announces a popup.

    Emitted with NO sessionId, because `Target.targetCreated` is a browser-level event —
    that is precisely why the session short-circuit in `_on_event` cannot filter it and
    the `openerId` check has to.
    """
    real_send = browser.send

    def send_and_open(msg):
        real_send(msg)
        if (msg.get("method") == "Input.dispatchMouseEvent"
                and msg["params"]["type"] == "mouseReleased"):
            browser.emit("Target.targetCreated", {"targetInfo": info})
    browser.send = send_and_open


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
    browser.eval_hook = lambda e: (["interactive", 5, 900, 123, 20]
                                   if "hash >>> 0" in e else
                                   "https://a.test/" if "location" in e else None)
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


def test_goto_returns_when_a_page_is_usable_before_its_timeout(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.lifecycle_names = []
    browser.eval_hook = lambda e: (["interactive", 2, 900] if "readyState" in e
                                   else "https://a.test/" if "location" in e else None)
    started = time.monotonic()
    r = tab.goto("https://a.test/", timeout=2.0, usable_after=0.05)
    assert r["lifecycle"] == "usable"
    assert time.monotonic() - started < 0.5


def test_goto_can_require_the_exact_lifecycle_until_deadline(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.lifecycle_names = []
    browser.eval_hook = lambda e: (["interactive", 2, 900] if "readyState" in e
                                   else "https://a.test/" if "location" in e else None)
    started = time.monotonic()
    r = tab.goto("https://a.test/", timeout=0.15, usable_after=None)
    assert r["lifecycle"] == "timeout"
    assert time.monotonic() - started >= 0.1


def test_goto_strict_mode_does_not_accept_the_settled_pair(wired):
    """`None` disables both early exits, not only the usable-document timer."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.lifecycle_names = ["DOMContentLoaded", "networkAlmostIdle"]
    browser.eval_hook = lambda e: (["interactive", 2, 900, 123, 20]
                                   if "hash >>> 0" in e else
                                   "https://a.test/" if "location" in e else None)
    started = time.monotonic()

    r = tab.goto("https://a.test/", timeout=0.15, usable_after=None)

    assert r == {"requested": "https://a.test/", "landed": "https://a.test/",
                 "lifecycle": "timeout"}
    assert time.monotonic() - started >= 0.1


def test_navigation_journals_bounded_timing_evidence(wired, tmp_path):
    browser, conn, registry = wired
    path = tmp_path / "navigation.jsonl"
    tab = Tab(conn, registry, "a", journal=Journal(path, session="nav"))
    browser.lifecycle_names = ["DOMContentLoaded", "networkAlmostIdle"]
    browser.eval_hook = lambda e: (["interactive", 2, 900, 123, 20]
                                   if "hash >>> 0" in e else
                                   "https://a.test/" if "location" in e else None)

    tab.goto("https://a.test/", timeout=1.0, usable_after=0.05)

    entries = [json.loads(line) for line in path.read_text().splitlines()]
    note = next(row for row in entries if row.get("event") == "navigation_wait")
    assert note["lifecycle"] == "settled"
    assert note["effective_usable_after"] == 0.05
    assert note["readiness_probes"] == 2
    assert note["parsed_ready_ms"] is not None
    assert note["critical_requests_peak"] == 0


def test_navigation_grace_adapts_within_documented_session_bounds(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.lifecycle_names = ["DOMContentLoaded", "load"]
    browser.eval_hook = lambda e: (["interactive", 2, 900, 123, 20]
                                   if "hash >>> 0" in e else
                                   "https://a.test/" if "location" in e else None)

    # Two exact, fast documents train this Tab only; no URL/origin is retained.
    tab.goto("https://a.test/one")
    tab.goto("https://a.test/two")
    assert tab._adaptive_navigation_grace(10.0) == (0.5, 2)
    assert tab._adaptive_navigation_grace(0.05) == (0.05, 2)
    other = _tab(wired)
    try:
        assert other._adaptive_navigation_grace(3.0) == (3.0, 0)
    finally:
        other.close()

    browser.lifecycle_names = ["DOMContentLoaded"]
    started = time.monotonic()
    r = tab.goto("https://a.test/stalled", timeout=2.0)

    assert r["lifecycle"] == "usable"
    assert 0.45 <= time.monotonic() - started < 1.2


def test_navigation_waits_for_observed_delayed_spa_data(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.lifecycle_names = ["DOMContentLoaded", "load"]
    state = {"complete": False}

    def evaluate(expression):
        if "maxChars" in expression:
            text = "complete application data" if state["complete"] else "loading"
            return {"url": "https://a.test/spa", "title": "SPA", "text": text,
                    "links": [], "challenge": {"detected": False}}
        if "hash >>> 0" in expression:
            return (["interactive", 2, 900, 222, 30] if state["complete"]
                    else ["interactive", 0, 7, 111, 8])
        return "https://a.test/spa" if "location" in expression else None

    browser.eval_hook = evaluate
    tab.goto("https://a.test/train-one")
    tab.goto("https://a.test/train-two")
    browser.lifecycle_names = []
    real_send = browser.send

    def send_with_delayed_data(message):
        real_send(message)
        if message.get("method") != "Page.navigate":
            return
        sid = message.get("sessionId")
        browser.emit("Page.lifecycleEvent",
                     {"name": "DOMContentLoaded", "loaderId": "L1", "frameId": "F1"},
                     session_id=sid)
        browser.emit("Network.requestWillBeSent",
                     {"requestId": "data-1", "loaderId": "", "frameId": "F1",
                      "type": "Fetch"}, session_id=sid)
        browser.emit("Page.lifecycleEvent",
                     {"name": "networkAlmostIdle", "loaderId": "L1", "frameId": "F1"},
                     session_id=sid)

        def finish():
            time.sleep(0.7)
            state["complete"] = True
            browser.emit("Network.loadingFinished", {"requestId": "data-1"},
                         session_id=sid)

        threading.Thread(target=finish, daemon=True).start()

    browser.send = send_with_delayed_data
    started = time.monotonic()

    out = tab.open_page("https://a.test/spa", timeout=2.0)

    elapsed = time.monotonic() - started
    assert out["lifecycle"] == "settled"
    assert out["page"]["text"] == "complete application data"
    assert 0.7 <= elapsed < 1.5


def test_open_page_folds_digest_into_the_landing_check(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    page = {"url": "https://a.test/landed", "title": "A", "text": "hello",
            "links": [], "challenge": {"detected": False}}
    browser.eval_hook = lambda e: page if "maxChars" in e else None
    before = len([c for c in browser.calls if c.get("method") == "Runtime.evaluate"])
    out = tab.open_page("https://a.test/")
    after = len([c for c in browser.calls if c.get("method") == "Runtime.evaluate"])
    assert after - before == 1
    assert out["landed"] == page["url"] and out["page"] == page


def test_page_text_is_bounded_by_default_and_supports_paging(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    expressions = []
    browser.eval_hook = lambda e: expressions.append(e) or {
        "url": "https://a.test/", "title": "A", "document_id": 1,
        "text": "window", "text_start": 12_000,
        "blocks": [{"kind": "paragraph", "key": "body>p:1",
                    "text": "x" * 12_000 + "window"}],
    }
    assert tab.page_text(start=12_000) == "window"
    assert "start = 12000" in expressions[-1]


def test_page_text_remains_repeatable_while_structured_reads_dedupe(wired):
    """The legacy string helper cannot return block refs, so semantic dedupe must not
    turn a second call into a silent empty-page result."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: {
        "url": "https://a.test/", "title": "A", "document_id": 1,
        "text": "same text", "blocks": [
            {"kind": "paragraph", "key": "body>p:1", "text": "same text"},
        ],
    }

    assert tab.page_text() == "same text"
    assert tab.page_text() == "same text"


def test_read_page_supports_paging_without_a_second_helper(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    expressions = []
    browser.eval_hook = lambda e: expressions.append(e) or {
        "text": "window", "text_start": 6_000, "text_remaining": 12_000,
    }

    out = tab.read_page(max_chars=3_000, start=6_000)

    assert out["text"] == "window"
    assert "start = 6000" in expressions[-1]
    assert "raw.slice(start, start + maxChars)" in expressions[-1]


def test_diagnostics_does_not_reenable_default_session_domains(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    before = len(browser.calls)
    out = tab.start_diagnostics()
    methods = [c.get("method") for c in browser.calls[before:]]
    assert methods == ["Log.enable", "Performance.enable"]
    assert out["enabled"] == ["Log", "Performance"]


def test_wait_lifecycle_wakes_on_the_event_not_on_a_poll(wired):
    browser, _, _ = wired
    tab = _tab(wired)
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


def test_action_consequence_never_calls_an_unrelated_mutation_success(wired):
    tab = _tab(wired)
    consequence = tab._shape_action_consequence({
        "mutation_count": 1,
        "changed_regions": [{"kind": "paragraph", "text": "clock tick",
                             "related": False}],
        "related_regions": 0,
    })

    assert consequence["effect"] == "unverified_mutation"
    assert consequence["verified"] is False


def test_action_consequence_never_calls_a_no_op_success(wired):
    tab = _tab(wired)

    consequence = tab._shape_action_consequence({})

    assert consequence["effect"] == "none"
    assert consequence["verified"] is False


def test_action_consequence_types_modal_and_validation_evidence(wired):
    tab = _tab(wired)
    modal = tab._shape_action_consequence({
        "mutation_count": 1, "modal": True,
        "changed_regions": [{"kind": "modal", "text": "Choose one",
                             "related": True}],
        "related_regions": 1,
    })
    validation = tab._shape_action_consequence(
        {"states": {"e1": {"value": "new", "valid": False,
                             "validationMessage": "Required"}}},
        before_states={"e1": {"value": "", "valid": True,
                              "validationMessage": ""}},
    )

    assert modal["effect"] == "modal" and modal["verified"] is True
    assert validation["effect"] == "validation" and validation["verified"] is True
    assert validation["validation_changed"] == ["e1"]


def test_an_unrelated_modal_is_exposed_but_never_called_success(wired):
    tab = _tab(wired)
    modal = tab._shape_action_consequence({
        "mutation_count": 1, "modal": True,
        "changed_regions": [{"kind": "modal", "text": "Unrelated",
                             "related": False}],
        "related_regions": 0,
    })

    assert modal["effect"] == "modal"
    assert modal["verified"] is False


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


def test_frames_zero_probe_uses_one_way_observation_not_the_reannouncement_dance(wired):
    """A trustworthy zero skips the off/on reannouncement dance. It arms auto-attach
    once so a shortly inserted child can wake the bounded observation window."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.frame_host_count = 0
    assert tab.frames() == []
    dances = [c for c in browser.calls if c.get("method") == "Target.setAutoAttach"]
    assert [c["params"]["autoAttach"] for c in dances] == [True]
    searches = [c for c in browser.calls if c.get("method") == "DOM.performSearch"]
    assert searches[0]["params"] == {
        "query": "iframe,frame,object,embed", "includeUserAgentShadowDOM": True,
    }
    # A search that matched nothing retains nothing, so its handle is released in a batch
    # rather than costing a blocking round trip on every frameless page.
    assert sum(c.get("method") == "DOM.discardSearchResults" for c in browser.calls) == 0


def test_a_frameless_page_does_not_ask_the_document_about_same_site_iframes(wired):
    """`FRAME_HOST_QUERY` includes `iframe` and the search pierces closed shadow roots,
    which `querySelectorAll` does not — so a trustworthy zero has already answered the
    same-site question with more reach than the follow-up evaluation could."""
    browser, _, _ = wired
    tab = _tab(wired)
    tab._ensure_world()
    browser.frame_host_count = 0
    mark = len(browser.calls)
    assert tab.frames() == []
    assert [c.get("method") for c in browser.calls[mark:]] == [
        "DOM.performSearch", "Target.setAutoAttach"]


def test_empty_search_handles_are_released_once_the_batch_fills(wired):
    """Deferred is not leaked: the handles pile up to a cap and then cost one round trip
    for all of them, so a script looping on one long-lived tab stays bounded."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.frame_host_count = 0
    for _ in range(Tab._SEARCH_FLUSH_AT):
        tab.frames()
    discards = [c for c in browser.calls if c.get("method") == "DOM.discardSearchResults"]
    assert len(discards) == Tab._SEARCH_FLUSH_AT      # one flush, every handle released
    assert not tab._pending_searches


def test_frames_catches_an_oopif_inserted_shortly_after_a_zero_probe(wired):
    """The fast probe is an instant, not a stability claim. Real Chrome reproduced this
    with an SPA inserting a cross-site iframe 80ms later: the old first call returned in
    ~5ms and only a second call could see the OOPIF."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.frame_host_count = 0
    timer = threading.Timer(0.08, lambda: browser.emit(
        "Target.attachedToTarget",
        {"targetInfo": {"targetId": "late-frame", "type": "iframe",
                        "url": "https://late-frame.test/"}},
        session_id=tab._session_id))
    timer.start()
    started = time.monotonic()
    got = tab.frames()
    elapsed = time.monotonic() - started
    timer.join()

    assert [f["target_id"] for f in got] == ["late-frame"]
    assert 0.05 < elapsed < 0.5
    calls = [c for c in browser.calls if c.get("method") == "Target.setAutoAttach"]
    assert [c["params"]["autoAttach"] for c in calls] == [True]


def test_frames_runs_the_dance_when_pierced_search_sees_a_child(wired):
    """A child host inside a closed shadow root is visible to DOM.performSearch even
    though page JavaScript sees ``host.shadowRoot === null`` (measured live)."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.frame_host_count = 1
    threading.Timer(0.02, lambda: browser.emit(
        "Target.attachedToTarget",
        {"targetInfo": {"targetId": "f0", "type": "iframe",
                        "url": "https://x0.test/"}},
        session_id=tab._session_id)).start()
    got = tab.frames()
    assert [f["target_id"] for f in got] == ["f0"]
    dances = [c for c in browser.calls if c.get("method") == "Target.setAutoAttach"]
    assert len(dances) == 2


def test_frames_fails_open_when_the_probe_fails(wired):
    """A frame report that says "none" must be earned. A failed probe means unknown, and
    unknown runs the dance: failing closed here silently dropped OOPIFs on exactly the
    bot-walled pages frames() exists for."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.frame_host_error = "DOM search unavailable"
    threading.Timer(0.02, lambda: browser.emit(
        "Target.attachedToTarget",
        {"targetInfo": {"targetId": "f0", "type": "iframe",
                        "url": "https://x0.test/"}},
        session_id=tab._session_id)).start()
    got = tab.frames()
    assert [f["target_id"] for f in got] == ["f0"]


def test_frames_fails_open_when_the_probe_answer_is_untrusted(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.frame_host_count = None
    threading.Timer(0.02, lambda: browser.emit(
        "Target.attachedToTarget",
        {"targetInfo": {"targetId": "f0", "type": "iframe",
                        "url": "https://x0.test/"}},
        session_id=tab._session_id)).start()
    assert [f["target_id"] for f in tab.frames()] == ["f0"]


def test_frames_collects_every_announcement_not_just_the_first(wired):
    """The old wait returned on the FIRST announcement and read the buffer immediately, so
    a page with several OOPIFs reported only the ones that had arrived by then. Silent
    under-reporting: the caller saw a short list and no indication it was short."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.frame_host_count = 3
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
        if "PointerEvent" in e:
            state["clicked"] = True
            return True
        if "getBoundingClientRect" in e:
            return [10.0, 20.0, "https://a.test/", 0, None,
                    {"tag": "button", "focused": False}]
        if "location.href" in e:
            return (["https://a.test/after", 0, {"tag": "button", "focused": True}]
                    if state["clicked"] else
                    ["https://a.test/", 0, {"tag": "button", "focused": False}])
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
        if "PointerEvent" in e:
            fired["dom"] = True
            return True
        if "getBoundingClientRect" in e:
            return [10.0, 20.0, "https://a.test/", 0, None,
                    {"tag": "button", "focused": False}]
        if "location.href" in e:
            return ["https://a.test/", 7, {"tag": "button", "focused": True}]
        return None

    browser.eval_hook = hook
    d = tab.click_ref("e1", settle=0.01)
    assert fired["dom"] is False
    assert d["modality"] == "compositor"
    assert d["dom_mutations"] == 7


def test_a_native_control_state_change_is_never_repeated(wired):
    """A checkbox toggle does not necessarily mutate the DOM. Its property state is
    nevertheless direct evidence that the compositor click landed, so a DOM retry would
    toggle it back to the original value."""
    browser, _, _ = wired
    tab = _tab(wired)
    fired = {"dom": False}

    def hook(e):
        if "PointerEvent" in e:
            fired["dom"] = True
            return True
        if "getBoundingClientRect" in e:
            return [10.0, 20.0, "https://a.test/", 0, None,
                    {"tag": "input", "type": "checkbox", "focused": False,
                     "checked": False}]
        if "location.href" in e:
            return ["https://a.test/", 0,
                    {"tag": "input", "type": "checkbox", "focused": True,
                     "checked": True}]
        return None

    browser.eval_hook = hook
    delta = tab.click_ref("e1", settle=0.01)
    assert fired["dom"] is False
    assert delta["modality"] == "compositor"
    assert delta["control_state_changed"] is True


def test_focus_alone_does_not_suppress_an_inert_click_retry(wired):
    """Mouse press can focus a button even when its activation is dropped. Focus is useful
    observation, but it is not evidence that the requested action happened."""
    browser, _, _ = wired
    tab = _tab(wired)
    fired = {"dom": False}

    def hook(e):
        if "PointerEvent" in e:
            fired["dom"] = True
            return True
        if "getBoundingClientRect" in e:
            return [10.0, 20.0, "https://a.test/", 0, None,
                    {"tag": "button", "type": "button", "focused": False}]
        if "location.href" in e:
            return ["https://a.test/", 0,
                    {"tag": "button", "type": "button", "focused": True}]
        return None

    browser.eval_hook = hook
    delta = tab.click_ref("e1", settle=0.01)

    assert fired["dom"] is True
    assert delta["modality"] == "dom"
    assert delta["control_state_changed"] is False


# --- a session is a lease; the target is the identity -------------------------

def _settle(cond, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_a_stale_session_recovers_by_reattaching_to_the_living_target(wired):
    """The browser detaching a SESSION does not mean the TAB died — a lease expired, not
    the thing it named. `ensure_live` takes a new lease on the same target and the caller
    never notices. Safe precisely because callers hold target ids: the replacement is for
    the target they named, by construction, so there is no tab to be redirected to —
    which is why none of browser-use PR 618's session-replacement machinery exists here."""
    browser, _conn, registry = wired
    tab = _tab(wired)
    first = tab._session_id
    browser.eval_hook = lambda e: "ok"
    browser.emit("Target.detachedFromTarget", {"sessionId": first})
    assert _settle(lambda: not registry._sessions["a"].live)
    assert tab.js("1") == "ok"                    # recovered mid-flight, no raise
    assert tab._session_id != first               # a NEW lease, same target


def test_recovery_rearms_the_injected_scripts_on_the_new_session(wired):
    """Everything the old session carried — SAFETY_JS registration, the isolated-world
    runtime, the wait binding — dies with it and announces nothing: the next navigation
    would simply load WITHOUT the dry-run guard. The re-arm is therefore a safety
    property, not a nicety."""
    browser, _conn, registry = wired
    tab = _tab(wired)
    first = tab._session_id
    scripts_before = [c for c in browser.calls
                      if c.get("method") == "Page.addScriptToEvaluateOnNewDocument"]
    assert len(scripts_before) == 2               # SAFETY_JS + RUNTIME_JS on attach
    browser.eval_hook = lambda e: "ok"
    browser.emit("Target.detachedFromTarget", {"sessionId": first})
    assert _settle(lambda: not registry._sessions["a"].live)
    tab.js("1")
    new_sid = tab._session_id
    rearmed = [c for c in browser.calls
               if c.get("method") == "Page.addScriptToEvaluateOnNewDocument"
               and c.get("sessionId") == new_sid]
    assert new_sid != first and len(rearmed) == 2


def test_a_destroyed_target_still_fails_closed(wired):
    """Recovery is for expired leases only. A destroyed target has nothing to re-attach
    to, and pretending otherwise would fabricate a tab."""
    browser, _conn, registry = wired
    tab = _tab(wired)
    browser.destroy("a")
    assert _settle(lambda: "a" not in registry._sessions
                   or not registry._sessions["a"].live)
    with pytest.raises(HarnessError):
        tab.js("1")


def test_concurrent_stale_recovery_installs_one_replacement_session(wired):
    """Two callers observing one stale generation must share one replacement. Previously
    both forgot the stale session outside the target lock; the loser could then delete
    the winner's fresh session and attach a third generation."""
    browser, _conn, registry = wired
    first = registry.ready_session("a")
    registry.mark("a", State.SESSION_STALE, "test detach")
    browser.latency = 0.02
    start = threading.Barrier(3)
    returned = []

    def recover():
        start.wait()
        returned.append(registry.ensure_live("a").session_id)

    threads = [threading.Thread(target=recover) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert len(returned) == 2
    assert returned[0] == returned[1] != first.session_id
    assert browser.attach_count["a"] == 2


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
    assert {key: out[key] for key in ("chars", "modality", "delivered")} == {
        "chars": 3, "modality": "dom", "delivered": 0,
    }
    assert out["consequence"]["effect"] == "input_delivery"
    assert out["consequence"]["verified"] is True


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


def test_a_terminal_application_state_owes_the_page_no_teardown(wired):
    """The watch script disconnects and forgets its observer before reporting a match, so
    the teardown evaluation that used to run unconditionally asked the page to delete
    something that was already gone. Measured on the 2026-08-25 corpus: one wasted
    `Runtime.evaluate` per call, 174 of `wait_for_application_state`'s 516."""
    browser, _, _ = wired
    tab = _tab(wired)
    tab._ensure_world()                        # pay the world once, outside the measurement
    browser.eval_hook = lambda e: (
        {"matched": True, "immediate": True, "state": "form", "fields": 8,
         "controls": 11, "text_len": 844, "title": "Job",
         "url": "https://jobs.test/apply", "ready_state": "complete"}
        if "hasSubmit" in e else None)

    mark = len(browser.calls)
    assert tab.wait_for_application_state(timeout=2.0)["state"] == "form"
    assert [c.get("method") for c in browser.calls[mark:]] == ["Runtime.evaluate"]


def test_an_abandoned_observer_is_still_torn_down(wired):
    """The other half: a wait that gives up left an observer running, and a MutationObserver
    on a busy page runs its callback for the life of the document. That one must still cost
    a round trip."""
    browser, _, _ = wired
    tab = _tab(wired)
    tab._ensure_world()
    browser.eval_hook = lambda e: (
        {"matched": False, "immediate": False, "state": "loading", "fields": 0,
         "controls": 0, "text_len": 0, "title": "Job", "url": "https://a.test/",
         "ready_state": "complete"}
        if "hasSubmit" in e else None)

    mark = len(browser.calls)
    result = tab.wait_for_application_state(
        timeout=0.5, usable_stable=0.05, empty_stable=0.05)
    assert result["state"] == "stable_failure"
    evaluated = [c for c in browser.calls[mark:] if c.get("method") == "Runtime.evaluate"]
    assert "__bh.watch" in json.dumps(evaluated[-1]["params"])


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
        [15.0, 25.0, "https://a.test/", 5] if "getBoundingClientRect" in e
        else ["https://a.test/", 9, None])
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
        [10.0, 10.0, "https://a.test/", 3] if "getBoundingClientRect" in e
        else ["https://a.test/next", 0, None])
    delta = tab.click_ref("e1", settle=0.05)
    assert delta["navigated"] is True
    assert delta["dom_mutations"] is None            # a new document restarts the counter


def test_a_click_that_opens_a_new_tab_reports_the_target(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = _click_hook()
    # `openerId` is what makes this popup THIS tab's: Chrome sets it on the target the
    # click opened, and only on that one.
    _open_on_release(browser, {"type": "page", "targetId": "popup1",
                               "openerId": "a"})
    delta = tab.click_ref("e1", settle=0.3)
    assert delta["new_targets"] == ["popup1"]


def test_a_foreign_tab_never_reaches_this_tabs_click_delta(wired):
    """`Target.targetCreated` is browser-level and carries no sessionId, so the session
    short-circuit at the top of `_on_event` cannot filter it: without an `openerId` check
    every Tab in the process recorded every page target the whole browser opened — other
    `parallel()` workers' tabs, other `bh` processes' tabs, the user's own browsing.
    `follow_application` then does `use_tab(new_targets[-1])`, so worker A went on to fill
    its form in worker B's tab.
    """
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = _click_hook()
    _open_on_release(browser, {"type": "page", "targetId": "another-workers-tab",
                               "openerId": "some-other-target"})
    delta = tab.click_ref("e1", settle=0.3)
    assert delta["new_targets"] == []


def test_a_foreign_popup_does_not_end_the_wait_before_this_tabs_popup(wired):
    """Filtering storage is too late: the waiter itself must ignore the foreign browser-
    level event or the click returns before its own popup arrives and may retry the action."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = _click_hook()
    real_send = browser.send
    timers: list[threading.Timer] = []

    def send_foreign_then_owned(msg):
        real_send(msg)
        if (msg.get("method") == "Input.dispatchMouseEvent"
                and msg["params"]["type"] == "mouseReleased"):
            browser.emit("Target.targetCreated", {"targetInfo": {
                "type": "page", "targetId": "foreign-popup",
                "openerId": "some-other-target"}})
            timer = threading.Timer(0.08, lambda: browser.emit(
                "Target.targetCreated", {"targetInfo": {
                    "type": "page", "targetId": "owned-popup",
                    "openerId": tab.target_id}}))
            timer.start()
            timers.append(timer)

    browser.send = send_foreign_then_owned
    started = time.monotonic()
    delta = tab.click_ref("e1", settle=0.3)
    elapsed = time.monotonic() - started
    for timer in timers:
        timer.join()

    assert elapsed >= 0.05
    assert delta["new_targets"] == ["owned-popup"]


def test_a_foreign_tab_does_not_suppress_this_tabs_inert_click_retry(wired):
    """The same leak corrupted the retry guard from the other side: a tab opening anywhere
    in the browser made this click look as though it had done something, so the
    DOM-activation retry — the only thing that rescues a click a background renderer
    dropped — was skipped for a click that had in fact been inert."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = _click_hook(activates=True)
    _open_on_release(browser, {"type": "page", "targetId": "another-workers-tab",
                               "openerId": "some-other-target"})
    delta = tab.click_ref("e1", settle=0.3)
    assert delta["modality"] == "dom"                  # the retry ran
    assert delta["new_targets"] == []


def test_a_popup_is_still_reported_after_sixteen_targets_have_been_seen(wired):
    """`_created` is bounded at 16 — a runaway popup loop must not grow it without limit —
    and its LENGTH was the click's cursor. Once the buffer saturated, `len()` stopped
    changing, so every subsequent click's slice was empty: popups silently stopped being
    followed for the rest of the process's life, and the inert-click guard read as "no new
    target" forever. Bounding the buffer is right; taking a position off a bounded length
    is not.
    """
    browser, _, _ = wired
    tab = _tab(wired)
    for n in range(20):
        browser.emit("Target.targetCreated",
                     {"targetInfo": {"type": "page", "targetId": f"earlier{n}",
                                     "openerId": tab.target_id}})
    _reader_caught_up(tab)
    assert len(tab._created) == 16                     # bounded, and now saturated

    browser.eval_hook = _click_hook()
    _open_on_release(browser, {"type": "page", "targetId": "popup1",
                               "openerId": tab.target_id})
    delta = tab.click_ref("e1", settle=0.3)
    assert delta["new_targets"] == ["popup1"]


def test_the_dialog_dance_a_blocked_dispatch_is_a_click_that_opened_a_dialog(wired):
    """`Input.dispatchMouseEvent` does not ACK while the click handler's dialog is up.
    That must be reported as a successful click with a dialog, not as a hang."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: (
        [10.0, 10.0, "https://a.test/", 0] if "getBoundingClientRect" in e
        else ["https://a.test/", 0, None])
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
        [10.0, 10.0, "https://a.test/", 0] if "getBoundingClientRect" in e
        else ["https://a.test/", 0, None])
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


def test_an_uninvited_dialog_is_auto_dismissed_so_the_renderer_unblocks(wired):
    """A dialog that opened with no click in flight — an alert from a page timer, a
    confirm on load — used to have NO resolver at all: only the click dance dismissed
    dialogs, so the renderer stayed blocked and every subsequent call on the tab timed
    out. Measured under parallel() as one worker per run dying at exactly 45.0s: a 25s
    Page.navigate timeout plus a 20s Runtime.evaluate timeout on a renderer that would
    never answer either. After the grace period the accept_dialogs policy applies."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.emit("Page.javascriptDialogOpening",
                 {"type": "alert", "message": "surprise"},
                 session_id=tab._session_id)
    deadline = time.monotonic() + 2
    handled = []
    while time.monotonic() < deadline and not handled:
        handled = [c for c in browser.calls
                   if c.get("method") == "Page.handleJavaScriptDialog"]
        time.sleep(0.01)
    assert handled and handled[0]["params"]["accept"] is False   # policy default
    assert tab._dialog is None                                   # unblocked and cleared


def test_the_grace_period_lets_the_click_dance_win_its_own_dialog(wired):
    """The auto-resolver must not steal a dialog the click dance is about to claim: the
    dance reads richer context (it reports the dialog in the click's delta). Within the
    grace window the dance resolves it; the auto-resolver then finds the claim and does
    nothing."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: (
        [10.0, 10.0, "https://a.test/", 0] if "getBoundingClientRect" in e
        else ["https://a.test/", 0, None])
    browser.emit("Page.javascriptDialogOpening", {"type": "confirm", "message": "Now?"},
                 session_id=tab._session_id)
    time.sleep(0.05)                       # inside the grace window
    delta = tab.click_ref("e1", settle=0.01)
    assert delta["dialog"] == {"type": "confirm", "message": "Now?"}
    time.sleep(Tab.DIALOG_GRACE + 0.2)     # let the auto-resolver wake and stand down
    handled = [c for c in browser.calls
               if c.get("method") == "Page.handleJavaScriptDialog"]
    assert len(handled) == 1               # exactly one dismissal, the dance's


def test_a_dialog_opened_by_the_fallback_click_still_reaches_the_delta(wired):
    """The DOM retry runs the handler the dropped compositor click never reached —
    including one that opens a dialog. The report used to be finalized BEFORE the retry,
    so that dialog vanished from the delta: measured on a hidden tab with an
    alert-opening button, the handler fired exactly once, the auto-resolver dismissed
    the alert, and the delta said dialog: null. daemon_check's event check caught it,
    but only when tab-adoption ordering happened to hand the client a hidden tab."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: (
        # the pre-click evaluate scrolls the ref into view; the post-click one does not
        [10.0, 10.0, "https://a.test/", 0, None, None] if "scrollIntoView" in e
        else True if "PointerEvent" in e
        else ["https://a.test/", 0, None])
    real_send = browser.send

    def dialog_on_gesture(msg):
        real_send(msg)
        expr = str((msg.get("params") or {}).get("expression", ""))
        if msg.get("method") == "Runtime.evaluate" and "PointerEvent" in expr:
            browser.emit("Page.javascriptDialogOpening",
                         {"type": "alert", "message": "from-the-retry"},
                         session_id=tab._session_id)

    browser.send = dialog_on_gesture
    delta = tab.click_ref("e1", settle=0.01)
    assert delta["modality"] == "dom"
    assert delta["dialog"] == {"type": "alert", "message": "from-the-retry"}
    deadline = time.monotonic() + 2                # the auto-resolver still dismisses it
    while time.monotonic() < deadline and tab._dialog is not None:
        time.sleep(0.01)
    assert tab._dialog is None


def test_a_genuinely_hung_dispatch_still_raises(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: (
        [10.0, 10.0, "https://a.test/", 0] if "getBoundingClientRect" in e
        else ["https://a.test/", 0, None])
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


def test_short_lived_tabs_share_session_scoped_runtime_setup(wired):
    browser, _, _ = wired
    first = _tab(wired)
    first.close()
    second = _tab(wired)
    second.close()

    assert len(
        [
            call
            for call in browser.calls
            if call.get("method") == "Page.addScriptToEvaluateOnNewDocument"
        ]
    ) == 2
    assert len(
        [call for call in browser.calls if call.get("method") == "Runtime.addBinding"]
    ) == 1
    assert not [
        call
        for call in browser.calls
        if call.get("method") in {"Page.getFrameTree", "Page.createIsolatedWorld"}
    ], "read-only tab construction must not eagerly build an isolated world"


def test_diagnostic_domains_are_enabled_once_per_session(wired):
    browser, _, _ = wired
    first = _tab(wired)
    first.start_diagnostics()
    first.close()
    second = _tab(wired)
    second.start_diagnostics()
    second.close()

    for method in ("Log.enable", "Performance.enable"):
        assert len([call for call in browser.calls if call.get("method") == method]) == 1


# --- item 21: screenshot ------------------------------------------------------

def test_screenshot_scale_is_the_inverse_of_dpr(wired, tmp_path):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: ({"x": 0, "y": 0, "width": 1200, "height": 800,
                                    "dpr": 2, "u": "https://a.test/", "t": "A"}
                                   if "window.innerWidth" in e else None)
    out = tab.capture_screenshot(tmp_path / "shot.jpeg")
    call = next(c for c in browser.calls if c.get("method") == "Page.captureScreenshot")
    assert call["params"]["clip"]["scale"] == 0.5    # dpr 2 → CSS pixels out
    assert call["params"]["format"] == "jpeg" and call["params"]["quality"] == 70
    assert (tmp_path / "shot.jpeg").read_bytes() == b"fake-image-bytes"
    assert out["css_viewport"] == [1200, 800]
    assert out["cdp_calls"] == 2


def test_max_dim_lowers_the_scale_instead_of_resizing_after(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: ({"x": 0, "y": 0, "width": 1200, "height": 800,
                                    "dpr": 2} if "window.innerWidth" in e else None)
    tab.capture_screenshot(max_dim=600)              # css 1200 wide → scale 600/2400
    call = [c for c in browser.calls if c.get("method") == "Page.captureScreenshot"][-1]
    assert call["params"]["clip"]["scale"] == 0.25


def test_png_when_the_path_says_so(wired, tmp_path):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: ({"x": 0, "y": 0, "width": 1200, "height": 800,
                                    "dpr": 1} if "window.innerWidth" in e else None)
    tab.capture_screenshot(tmp_path / "s.png")
    call = [c for c in browser.calls if c.get("method") == "Page.captureScreenshot"][-1]
    assert call["params"]["format"] == "png" and "quality" not in call["params"]


def test_recording_context_rides_on_the_viewport_evaluation(wired, tmp_path):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: ({"x": 2, "y": 3, "width": 1200, "height": 800,
                                    "dpr": 1, "u": "https://a.test/", "t": "A",
                                    "box": [1.2, 2.8, 30, 10]}
                                   if "window.innerWidth" in e else None)
    before = len(browser.calls)
    out = tab.capture_screenshot(tmp_path / "s.jpg", include_context=True)
    methods = [c.get("method") for c in browser.calls[before:]]
    assert methods == ["Runtime.evaluate", "Page.captureScreenshot"]
    assert out["context"]["box"] == [1.2, 2.8, 30, 10]


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
        if "getBoundingClientRect" in e else ["https://a.test/", 5, None])
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


def test_a_subframe_navigation_does_not_kill_the_main_frames_world(wired):
    """Subframes announce themselves through `Page.frameNavigated` too — with `parentId`
    set — and an ATS posting fires one for every ad, tracker and embedded video it loads.
    Treating them all as document replacements threw away live worlds: measured over one
    run, 149 navigations produced 233 invalidations, so 84 rebuilds resurrected a world
    that had never died."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: []
    tab.snapshot()
    ctx, worlds = tab._world_ctx, len(browser.isolated_worlds)
    assert ctx is not None

    browser.emit("Page.frameNavigated",
                 {"frame": {"id": "AD1", "parentId": "F1", "url": "https://ads.test/"}},
                 session_id=tab._session_id)
    _reader_caught_up(tab)
    assert tab._world_ctx == ctx
    tab.snapshot()
    assert len(browser.isolated_worlds) == worlds          # nothing was rebuilt


def test_a_main_frame_navigation_clears_the_world_and_caches_its_frame_id(wired):
    """The frame id in the event is the one `_ensure_world` used to buy with a whole frame
    tree. The fake reports a DIFFERENT id from the one `Page.getFrameTree` gave at attach
    purely so this assertion can only be satisfied by the event — real Chrome keeps the
    main frame's id stable across its navigations, which is what makes caching it safe."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: []
    tab.snapshot()
    assert (tab._world_ctx, tab._main_frame) == (77, "F1")

    browser.emit("Page.frameNavigated",
                 {"frame": {"id": "F2", "url": "https://a.test/next"}},
                 session_id=tab._session_id)
    _reader_caught_up(tab)
    assert tab._world_ctx is None                # the world died with its document
    assert tab._main_frame == "F2"               # the frame it lived in did not


def test_a_known_main_frame_id_replaces_the_frame_tree_round_trip(wired):
    """`Page.getFrameTree` was issued on every rebuild to read one field —
    `frameTree.frame.id` — out of a reply that carries the whole tree, while
    `Page.frameNavigated` had already handed the reader thread that same id for free."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: []
    browser.emit("Page.frameNavigated", {"frame": {"id": "F2"}},
                 session_id=tab._session_id)
    _reader_caught_up(tab)
    assert tab._world_ctx is None

    mark = len(browser.calls)
    tab.snapshot()
    # The whole rebuild, exactly: create the world, then run the caller's expression in it.
    assert [c.get("method") for c in browser.calls[mark:]] == [
        "Page.createIsolatedWorld", "Runtime.evaluate"]
    created = browser.calls[mark]
    assert created["params"]["frameId"] == "F2"            # straight off the event


def test_a_cleared_context_invalidates_the_world_but_not_the_frame_it_lived_in(wired):
    """A world is a property of the document and dies with it; the frame is a property of
    the target and outlives every document loaded into it. Dropping both on this event
    would put the frame-tree round trip back on the next rebuild."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: []
    tab.snapshot()
    assert (tab._world_ctx, tab._main_frame) == (77, "F1")

    browser.emit("Runtime.executionContextsCleared", {}, session_id=tab._session_id)
    _reader_caught_up(tab)
    assert tab._world_ctx is None
    assert tab._main_frame == "F1"

    mark = len(browser.calls)
    tab.snapshot()
    assert [c.get("method") for c in browser.calls[mark:]] == [
        "Page.createIsolatedWorld", "Runtime.evaluate"]


def test_building_a_world_no_longer_re_injects_the_runtime_into_it(wired):
    """`createIsolatedWorld(worldName=W)` returns the SAME world
    `addScriptToEvaluateOnNewDocument(worldName=W)` populates, so the evaluate that used to
    follow every rebuild was a round trip that re-ran an idempotent script over itself."""
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: []
    browser.emit("Page.frameNavigated", {"frame": {"id": "F2"}},
                 session_id=tab._session_id)
    _reader_caught_up(tab)
    tab.snapshot()                                         # rebuilds the world
    assert not [c for c in browser.calls if c.get("method") == "Runtime.evaluate"
                and c["params"].get("expression") == RUNTIME_JS]


def test_an_isolated_world_that_answers_empty_injects_the_runtime_once_and_retries(wired):
    """Chrome sharing the world by name is measured behaviour, not a protocol guarantee,
    so `_world_js` heals a world that comes back unpopulated instead of paying for an
    injection before every call.

    The ReferenceError is fabricated here, and deliberately so: no expression the harness
    currently sends can produce it. SNAPSHOT_JS and RUNTIME_JS both open with
    `window.__bh || (window.__bh = {...})`, and every other reference is guarded by
    `window.__bh &&` / `window.__bh ?`, so an unpopulated world answers with
    `TypeError: bh.visible is not a function` or with a silently empty ref lookup — not
    with `__bh is not defined`. This test pins the mechanism; the trigger it keys on does
    not yet fire on the real symptom.
    """
    browser, _, _ = wired
    tab = _tab(wired)
    injections = {"n": 0}

    def hook(expression):
        if expression == RUNTIME_JS:
            injections["n"] += 1
            return None
        if injections["n"] == 0:                  # the empty world, before it is healed
            return {"__raw__": {"result": {"type": "undefined"}, "exceptionDetails": {
                "text": "Uncaught", "lineNumber": 0, "exception": {
                    "description": "ReferenceError: __bh is not defined\n  at <anon>"}}}}
        return [{"ref": "e1", "tag": "input"}]

    browser.eval_hook = hook
    assert tab.snapshot() == [{"ref": "e1", "tag": "input"}]
    assert injections["n"] == 1                   # healed once, not before every call
    evaluated = [c["params"] for c in browser.calls
                 if c.get("method") == "Runtime.evaluate"]
    assert [p["expression"] for p in evaluated[-3:]] == [SNAPSHOT_JS, RUNTIME_JS, SNAPSHOT_JS]
    assert {p.get("contextId") for p in evaluated[-3:]} == {77}   # all of it in the world


def test_a_page_error_that_is_not_an_empty_world_is_never_papered_over_by_injection(wired):
    """A real scripting bug must surface as itself, and must not re-run forever.

    The heal cannot key on error prose — "__bh is not defined" was unreachable, because this
    module's JS self-creates `__bh` or guards it, so an unpopulated world never says that.
    So the repair is attempted once per world and the ORIGINAL cause is what the caller
    sees; the second failure in the same world does not even try.
    """
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: {"__raw__": {
        "result": {"type": "undefined"},
        "exceptionDetails": {"text": "Uncaught", "lineNumber": 2, "exception": {
            "description": "TypeError: el.matches is not a function"}}}}
    with pytest.raises(JsException) as first:
        tab.snapshot()
    assert "el.matches is not a function" in str(first.value.args[0]), "the real cause"
    injections = [c for c in browser.calls if c.get("method") == "Runtime.evaluate"
                  and c["params"].get("expression") == RUNTIME_JS]
    assert len(injections) == 1, "one repair attempt, not zero and not per-call"

    with pytest.raises(JsException):
        tab.snapshot()
    again = [c for c in browser.calls if c.get("method") == "Runtime.evaluate"
             and c["params"].get("expression") == RUNTIME_JS]
    assert len(again) == 1, "a healed world must never be re-injected"


# --- upload_file: the return must distinguish success from a wrong element ----

def test_uploads_can_be_disabled_before_any_file_or_browser_access(wired, monkeypatch):
    browser, _conn, _reg = wired
    tab = _tab(wired)
    monkeypatch.setenv("BH_DISABLE_FILE_UPLOADS", "yes")
    calls_before = len(browser.calls)

    with pytest.raises(SideEffectRefused) as error:
        tab.upload_file("e7", "/path/that/must/not/be-read.pdf")

    assert error.value.observed["ref"] == "e7"
    assert len(browser.calls) == calls_before


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
