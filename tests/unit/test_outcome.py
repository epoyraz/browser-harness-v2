"""The contract's own tests. Each asserts a rule that v1 broke in production."""
import copy
import json
import pickle

import pytest

from harness.core.outcome import (
    RECOVERY,
    Class,
    HarnessError,
    MappingOutcome,
    NavigationFailed,
    Tally,
    ValueRejected,
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


def test_mapping_outcome_preserves_legacy_json_and_the_outcome_contract():
    payload = {"attached": ["cv.pdf"], "requested": 1}
    out = MappingOutcome(ok(payload, **payload))
    assert isinstance(out, dict)
    assert json.loads(json.dumps(out)) == payload
    assert out.ok and out.value is out and out.unwrap() is out
    assert out.failures == []
    assert out.to_json()["observed"] == payload
    assert "value" not in out.to_json()


def test_mapping_outcome_keeps_plain_dict_copy_and_pickle_compatibility():
    out = MappingOutcome(ok({"attached": ["cv.pdf"]}, attached=["cv.pdf"]))
    for cloned in (copy.copy(out), copy.deepcopy(out), pickle.loads(pickle.dumps(out))):
        assert cloned == out and cloned.ok
        assert cloned.value is cloned and cloned.observed is cloned


def test_mapping_outcome_views_stay_aligned_after_mapping_mutation():
    out = MappingOutcome(ok({"attached": ["old.pdf"]}, attached=["old.pdf"]))
    out["attached"] = ["new.pdf"]
    assert out.value["attached"] == ["new.pdf"]
    assert out.observed["attached"] == ["new.pdf"]
    assert out.to_json()["observed"]["attached"] == ["new.pdf"]


def test_value_rejected_unwraps_to_its_own_typed_error():
    out = fail(Class.VALUE_REJECTED, "filtered", accept="application/pdf")
    with pytest.raises(ValueRejected) as error:
        out.unwrap()
    assert error.value.cls is Class.VALUE_REJECTED


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


def test_partial_carries_the_typed_failures_not_just_a_count():
    t = Tally()
    t.record(ok("a"))
    t.record(fail(Class.TIMEOUT, "slow", url="u2"))
    o = t.outcome()
    assert len(o.failures) == 1 and o.failures[0].cls is Class.TIMEOUT
    assert o.failures[0].observed["url"] == "u2"
    assert o.to_json()["failures"][0]["class"] == "timeout"


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


# --- the class says what went wrong; the recovery says what to do about it -------

def test_a_failure_carries_the_action_that_resolves_it():
    """A typed class replaced v1's prose a caller had to string-match. Without a recovery
    beside it, a fix the harness already knows costs a round trip to rediscover —
    `_step_class` in ops/forms.py had "the recovery is a different write mode" in a
    comment, where nothing could read it."""
    payload = fail(Class.NEEDS_INTERACTION, "a combobox has no value to set").to_json()
    assert "select_option" in payload["recovery"]


def test_success_carries_no_recovery():
    assert "recovery" not in ok(None).to_json()


def test_a_class_without_one_concrete_next_step_offers_none():
    """A recovery that depends on context is better served by `detail` than by a guess
    stated in the imperative."""
    assert "recovery" not in fail(Class.TIMEOUT, "took too long").to_json()
    assert "recovery" not in fail(Class.JS_EXCEPTION, "boom").to_json()


def test_every_recovery_is_addressed_to_the_caller():
    """Each entry names something the reader can do, not a description of the state."""
    assert RECOVERY, "the map must not be empty"
    for cls, text in RECOVERY.items():
        assert text and text[0].islower() or text[0].isupper(), cls
        assert len(text) < 240, f"{cls} reads as prose, not an instruction"


def test_the_doctor_and_the_outcome_tell_the_same_story():
    """Two maps keyed by the same enum drift. `bh --doctor`'s guidance is the shared one."""
    from harness.connect import doctor
    assert doctor.GUIDANCE is RECOVERY
    assert Class.ENDPOINT_UNREACHABLE in RECOVERY
