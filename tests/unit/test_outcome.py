"""The contract's own tests. Each asserts a rule that v1 broke in production."""
import pytest

from harness.core.outcome import (
    Class,
    HarnessError,
    NavigationFailed,
    Tally,
    fail,
    ok,
)

# --- rule 3: define success -------------------------------------------------
# v1's goto() returned a title for a chrome-error:// page, so "no exception" read as success.

def test_ok_is_explicit_not_inferred_from_absence_of_error():
    assert ok("value").ok is True
    assert fail(Class.NAVIGATION_FAILED).ok is False


def test_unwrap_raises_the_typed_error_not_a_generic_one():
    o = fail(Class.NAVIGATION_FAILED, "net::ERR_HTTP_RESPONSE_CODE_FAILURE",
             requested="https://x/careers", landed="chrome-error://chromewebdata/")
    with pytest.raises(NavigationFailed) as e:
        o.unwrap()
    # the caller branches on the class, never on the message
    assert e.value.cls is Class.NAVIGATION_FAILED
    assert e.value.observed["landed"].startswith("chrome-error://")


# --- rule 2: never discard a cause you were handed --------------------------

def test_evidence_survives_to_the_caller():
    o = fail(Class.NAVIGATION_FAILED, "net::ERR_HTTP_RESPONSE_CODE_FAILURE",
             requested="https://a", landed="chrome-error://chromewebdata/")
    assert o.observed["requested"] == "https://a"          # four-hop redirects make both load-bearing
    assert "ERR_HTTP_RESPONSE_CODE_FAILURE" in o.detail


def test_detail_is_never_the_thing_you_branch_on():
    # same class, different prose — a reworded Chrome message must not change behaviour
    a = fail(Class.SESSION_STALE, "Session with given id not found")
    b = fail(Class.SESSION_STALE, "no close frame received or sent")
    assert a.cls == b.cls
    assert HarnessError.of(a).__class__ is HarnessError.of(b).__class__


# --- retryability is stated by the party that knows -------------------------

def test_retryable_is_a_property_of_the_class():
    assert fail(Class.TIMEOUT).retryable is True
    assert fail(Class.SESSION_STALE).retryable is True
    assert fail(Class.NAVIGATION_FAILED).retryable is False   # a 404 will 404 again
    assert fail(Class.SCOPE_REFUSED).retryable is False       # #479: never retry into someone's browser


# --- rule 4: partial work is not success ------------------------------------
# an unbounded fan-out once returned 163 of ~300 results and reported success.

def test_tally_reports_all_three_counts():
    t = Tally()
    for i in range(163):
        t.record(ok(i))
    for _ in range(137):
        t.record(fail(Class.TIMEOUT, "throttled"))
    o = t.outcome()
    assert o.ok is False and o.cls is Class.PARTIAL
    assert o.observed == {"attempted": 300, "succeeded": 163, "failed": 137}


def test_partial_still_returns_what_it_got():
    t = Tally()
    t.record(ok("a")); t.record(fail(Class.TIMEOUT))
    o = t.outcome()
    assert o.value == ["a"]        # results are not thrown away, they are just not "success"
    assert o.ok is False


def test_a_clean_run_is_ok():
    t = Tally()
    for i in range(3):
        t.record(ok(i))
    o = t.outcome(passes=1)
    assert o.ok is True and o.value == [0, 1, 2] and o.observed["passes"] == 1


def test_empty_is_not_complete():
    # zero attempted must not read as "everything succeeded"
    assert Tally().complete is False


# --- rule 1: never invent a cause you did not verify ------------------------
# v1 mapped any handshake stall onto "click Allow", including a browser with zero windows.

def test_permission_and_no_window_are_distinct_classes():
    assert Class.PERMISSION_PENDING is not Class.NO_BROWSER_WINDOW
    assert Class.ENDPOINT_404 is not Class.ENDPOINT_UNREACHABLE


def test_json_shape_is_machine_readable():
    d = fail(Class.ENDPOINT_404, "no /json/version", port=9222).to_json()
    assert d["ok"] is False and d["class"] == "endpoint_404"
    assert d["observed"]["port"] == 9222 and d["retryable"] is False
    assert ok(1).to_json().get("retryable") is None   # only failures carry it
