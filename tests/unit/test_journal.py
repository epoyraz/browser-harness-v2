"""Journal tests. Each asserts something v1's 8-line, timestamp-free log could not do."""
import json

import pytest

from harness.core.journal import ELIDE_OVER, Journal, _elide
from harness.core.outcome import Class, NavigationFailed


@pytest.fixture
def j(tmp_path):
    return Journal(tmp_path / "s.jsonl", session="s1")


# --- the four fields that make one file serve three readers -----------------

def test_every_entry_is_orderable_and_correlatable(j):
    j.write("note", msg="a")
    j.write("note", msg="b")
    e = list(j.entries())
    assert len(e) == 2
    assert all({"ts", "id", "kind"} <= set(x) for x in e)
    assert e[0]["ts"] <= e[1]["ts"]          # v1's log had no timestamps at all


def test_client_and_daemon_entries_share_an_id(j):
    """The correlation v1 could not do: was the reattach before or after my call?"""
    with j.call("goto", url="https://x") as span:
        j.write("daemon", id=span.id, event="stale_session", to="https://x.com/home")
    ids = [e["id"] for e in j.entries()]
    assert ids[0] == ids[1]                  # daemon line joins the client call


# --- spans carry the actionable number --------------------------------------

def test_span_counts_cdp_round_trips(j):
    with j.call("fill_input", selector="#e", text="hello") as span:
        for _ in range(61):                  # 3 events per char + focus + events
            j.cdp("Input.dispatchKeyEvent")
        assert span.cdp_calls == 61
    entry = next(e for e in j.entries() if e["kind"] == "call")
    assert entry["cdp"] == 61                # visible without a benchmark
    assert entry["ms"] >= 0


def test_cdp_counts_against_the_innermost_span(j):
    with j.call("outer"):
        with j.call("inner") as inner:
            j.cdp("A")
        assert inner.cdp_calls == 1
    kinds = [e for e in j.entries() if e["kind"] == "call"]
    assert [k["fn"] for k in kinds] == ["inner", "outer"]   # inner closes first


def test_cdp_outside_a_span_is_not_an_error(j):
    j.cdp("Target.getTargets")               # never raise from observability
    assert j.current is None


# --- rule 2: never discard a cause you were handed --------------------------

def test_typed_failure_is_recorded_with_its_class_and_evidence(j):
    with pytest.raises(NavigationFailed), j.call("goto", url="https://x/careers"):
        raise NavigationFailed("net::ERR_HTTP_RESPONSE_CODE_FAILURE",
                               landed="chrome-error://chromewebdata/")
    out = next(e for e in j.entries() if e["kind"] == "call")["outcome"]
    assert out["ok"] is False
    assert out["class"] == Class.NAVIGATION_FAILED.value
    assert out["observed"]["landed"].startswith("chrome-error://")


def test_the_context_manager_never_swallows(j):
    with pytest.raises(ValueError), j.call("boom"):
        raise ValueError("x")


# --- elision keeps the journal diffable -------------------------------------

def test_large_payloads_are_replaced_by_a_digest():
    """One screenshot was 51 KB of a 54 KB session; a digest is enough for replay."""
    big = "x" * (ELIDE_OVER + 1)
    out = _elide({"data": big, "small": "ok"})
    assert out["small"] == "ok"
    assert out["data"]["_elided"] == len(big) and len(out["data"]["_sha256"]) == 16


def test_identical_payloads_elide_identically():
    a = _elide({"d": "y" * 5000})["d"]["_sha256"]
    b = _elide({"d": "y" * 5000})["d"]["_sha256"]
    assert a == b                            # replay compares requests, not bytes


def test_elision_reaches_into_nested_structures():
    out = _elide({"r": [{"data": "z" * 5000}]})
    assert out["r"][0]["data"]["_elided"] == 5000


# --- observability must never break the run ---------------------------------

def test_no_path_is_a_silent_no_op():
    j = Journal(None)
    with j.call("goto", url="x") as span:
        j.cdp("Page.navigate")
        assert span.cdp_calls == 1
    assert list(j.entries()) == []


def test_unwritable_path_degrades_instead_of_raising(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    j = Journal(blocked / "nested" / "s.jsonl")
    j.write("note", msg="still fine")        # must not raise
    assert list(j.entries()) == []


def test_a_truncated_final_line_is_skipped(tmp_path):
    p = tmp_path / "s.jsonl"
    j = Journal(p, session="s1")
    j.write("note", msg="good")
    with p.open("a") as fh:
        fh.write('{"ts": 1, "kind": "cal')   # killed mid-write
    assert [e["msg"] for e in j.entries()] == ["good"]


def test_ids_are_unique_and_monotonic(j):
    ids = [j.next_id() for _ in range(5)]
    assert ids == [f"s1.{i}" for i in range(1, 6)]
    assert len(set(ids)) == 5


def test_entries_are_one_json_object_per_line(tmp_path):
    p = tmp_path / "s.jsonl"
    j = Journal(p, session="s1")
    with j.call("goto", url="https://x"):
        pass
    lines = p.read_text().strip().split("\n")
    assert all(json.loads(line) for line in lines)
