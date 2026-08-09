"""Cassette tests. The property that matters: keyed by request signature, never by id."""
import json

import pytest

from harness.connect.cdp import Connection
from harness.connect.session import DEFAULT_DOMAINS, SessionRegistry
from harness.core.cassette import CassetteMiss, Player, Recorder, signature
from harness.core.journal import ELIDE_OVER
from tests.fake_browser import FakeBrowser


def _record(path, script):
    """Drive the fake browser through `script`, capturing the wire to `path`."""
    browser = FakeBrowser("a", "b")
    with Connection(Recorder(browser, path)) as conn:
        script(conn)
    return browser


# --- record → replay round trip ---------------------------------------------

def test_a_recorded_session_replays_without_a_browser(tmp_path):
    tape = tmp_path / "s.jsonl"

    def script(conn):
        registry = SessionRegistry(conn)
        session = registry.ready_session("a")
        assert conn.request("Runtime.evaluate", {"expression": "x"},
                            session_id=session.session_id)["result"]["value"] == "a"

    _record(tape, script)

    with Connection(Player(tape)) as conn:          # no FakeBrowser anywhere
        registry = SessionRegistry(conn)
        session = registry.ready_session("a")
        assert session.domains == DEFAULT_DOMAINS
        assert conn.request("Runtime.evaluate", {"expression": "x"},
                            session_id=session.session_id)["result"]["value"] == "a"


def test_a_worker_session_enables_runtime_without_page_only_domains():
    browser = FakeBrowser("worker")
    browser.targets["worker"]["type"] = "service_worker"
    with Connection(browser) as conn:
        session = SessionRegistry(conn).ready_session("worker")
    assert session.domains == ("Runtime",)
    assert browser.domains_for("worker") == ["Runtime"]


def test_replay_is_keyed_by_signature_not_by_id(tmp_path):
    """Ids are assigned by the client and shift whenever a code path changes. An id-keyed
    cassette would replay a different run's answers into the same slots and still look green.
    """
    tape = tmp_path / "s.jsonl"
    _record(tape, lambda c: [c.request("Runtime.evaluate", {"expression": e})
                             for e in ("first", "second")])

    with Connection(Player(tape)) as conn:
        # Reversed order: ids no longer line up, signatures still do.
        assert conn.request("Runtime.evaluate", {"expression": "second"})
        assert conn.request("Runtime.evaluate", {"expression": "first"})


def test_a_changed_request_misses_instead_of_passing_quietly(tmp_path):
    """This is the mechanism behind `bh replay --diff` (TODO 28)."""
    tape = tmp_path / "s.jsonl"
    _record(tape, lambda c: c.request("Runtime.evaluate", {"expression": "x"}))

    player = Player(tape)
    with pytest.raises(CassetteMiss) as e:
        player.send({"id": 1, "method": "Runtime.evaluate", "params": {"expression": "y"}})
    assert "different params" in str(e.value)


def test_an_extra_call_misses_rather_than_replaying_a_stale_answer(tmp_path):
    tape = tmp_path / "s.jsonl"
    _record(tape, lambda c: c.request("Runtime.evaluate", {"expression": "x"}))
    player = Player(tape)
    player.send({"id": 1, "method": "Runtime.evaluate", "params": {"expression": "x"}})
    with pytest.raises(CassetteMiss):
        player.send({"id": 2, "method": "Runtime.evaluate", "params": {"expression": "x"}})


def test_session_id_is_part_of_the_identity(tmp_path):
    """The same evaluate against two tabs is two requests. Conflating them lets a replayed
    test pass while driving the wrong target."""
    a = signature({"method": "Runtime.evaluate", "params": {"e": 1}, "sessionId": "S1"})
    b = signature({"method": "Runtime.evaluate", "params": {"e": 1}, "sessionId": "S2"})
    assert a != b


# --- events -----------------------------------------------------------------

def test_events_replay_ahead_of_the_response_they_preceded(tmp_path):
    tape = tmp_path / "s.jsonl"
    _record(tape, lambda c: SessionRegistry(c).ready_session("a"))

    seen = []
    with Connection(Player(tape)) as conn:
        conn.subscribe(seen.append)
        SessionRegistry(conn).ready_session("a")
    assert any(e.get("method") == "Target.attachedToTarget" for e in seen)


# --- size and the sidecar ----------------------------------------------------

def test_bulky_payloads_become_a_marker_plus_a_sidecar_blob(tmp_path):
    """The cassette line stays small and diffable; the bytes live in the sidecar."""
    tape = tmp_path / "s.jsonl"
    recorder = Recorder(FakeBrowser("a"), tape)
    recorder.send({"id": 1, "method": "Page.captureScreenshot",
                   "params": {"data": "Q" * (ELIDE_OVER + 1)}})
    frames = [json.loads(x) for x in tape.read_text().splitlines()]
    marker = frames[0]["params"]["data"]
    assert marker["_elided"] == ELIDE_OVER + 1
    assert len(tape.read_bytes()) < 400
    blob = tmp_path / "s.jsonl.blobs" / marker["_sha256"]
    assert blob.read_text() == "Q" * (ELIDE_OVER + 1)


def test_replay_delivers_the_original_bytes_not_the_marker(tmp_path):
    """Item 28's known break, fixed: an elided response handed to the replaying client
    crashes anything that decodes it. The Player reinflates from the sidecar."""
    tape = tmp_path / "s.jsonl"
    browser = FakeBrowser("a")
    big = "R" * (ELIDE_OVER * 3)
    browser.eval_hook = lambda e: big
    with Connection(Recorder(browser, tape)) as conn:
        assert conn.request("Runtime.evaluate", {"expression": "x"})["result"]["value"] == big
    with Connection(Player(tape)) as conn:
        got = conn.request("Runtime.evaluate", {"expression": "x"})["result"]["value"]
    assert got == big                               # byte-faithful, not a digest dict


def test_identical_payloads_dedupe_to_one_blob(tmp_path):
    tape = tmp_path / "s.jsonl"
    recorder = Recorder(FakeBrowser("a"), tape)
    for i in (1, 2):
        recorder.send({"id": i, "method": "M", "params": {"data": "Z" * 5000}})
    assert len(list((tmp_path / "s.jsonl.blobs").iterdir())) == 1


def test_a_missing_sidecar_degrades_to_the_marker_not_a_crash(tmp_path):
    import shutil
    tape = tmp_path / "s.jsonl"
    browser = FakeBrowser("a")
    browser.eval_hook = lambda e: "S" * 5000
    with Connection(Recorder(browser, tape)) as conn:
        conn.request("Runtime.evaluate", {"expression": "x"})
    shutil.rmtree(tmp_path / "s.jsonl.blobs")
    with Connection(Player(tape)) as conn:
        got = conn.request("Runtime.evaluate", {"expression": "x"})["result"]["value"]
    assert got["_elided"] == 5000                   # the marker, documented degrade


def test_signatures_match_across_the_sidecar_boundary(tmp_path):
    """A stored send holds the marker; a live send holds the raw string. They must hash
    identically, or every big-param request would miss on replay."""
    tape = tmp_path / "s.jsonl"
    big = "T" * 5000
    _record(tape, lambda c: c.request("Runtime.evaluate", {"expression": big}))
    with Connection(Player(tape)) as conn:
        assert conn.request("Runtime.evaluate", {"expression": big})


# --- golden-file diff (TODO 28) ----------------------------------------------

def test_identical_recordings_diff_equal(tmp_path):
    from harness.core.cassette import diff
    for name in ("a.jsonl", "b.jsonl"):
        _record(tmp_path / name, lambda c: c.request("Runtime.evaluate", {"expression": "x"}))
    report = diff(tmp_path / "a.jsonl", tmp_path / "b.jsonl")
    assert report["equal"] is True and report["first_divergence"] is None


def test_a_change_that_turns_1_round_trip_into_60_fails_the_diff(tmp_path):
    """TODO 28's done-when, verbatim."""
    from harness.core.cassette import diff
    _record(tmp_path / "golden.jsonl",
            lambda c: c.request("Runtime.evaluate", {"expression": "batch-fill"}))

    def v1_style(c):                                # per-character keystrokes
        for i in range(60):
            c.request("Input.dispatchKeyEvent", {"type": "char", "text": chr(97 + i % 26)})
    _record(tmp_path / "regressed.jsonl", v1_style)

    report = diff(tmp_path / "golden.jsonl", tmp_path / "regressed.jsonl")
    assert report["equal"] is False
    assert report["method_deltas"]["Input.dispatchKeyEvent"] == {"golden": 0, "got": 60}
    assert report["method_deltas"]["Runtime.evaluate"] == {"golden": 1, "got": 0}
    assert report["first_divergence"] == 0


def test_diff_pins_the_first_divergence(tmp_path):
    from harness.core.cassette import diff
    _record(tmp_path / "g.jsonl", lambda c: [c.request("Runtime.evaluate", {"expression": e})
                                             for e in ("a", "b", "c")])
    _record(tmp_path / "o.jsonl", lambda c: [c.request("Runtime.evaluate", {"expression": e})
                                             for e in ("a", "X", "c")])
    report = diff(tmp_path / "g.jsonl", tmp_path / "o.jsonl")
    assert report["first_divergence"] == 1 and report["method_deltas"] == {}


def test_recording_never_alters_the_traffic(tmp_path):
    browser = FakeBrowser("a")
    with Connection(Recorder(browser, tmp_path / "s.jsonl")) as conn:
        session = SessionRegistry(conn).ready_session("a")
    assert browser.domains_for("a") == list(DEFAULT_DOMAINS)
    assert session.live


def test_exhausted_reports_whether_the_replay_took_the_recorded_path(tmp_path):
    tape = tmp_path / "s.jsonl"
    _record(tape, lambda c: [c.request("Runtime.evaluate", {"expression": e})
                             for e in ("a", "b")])
    player = Player(tape)
    assert player.exhausted is False
    player.send({"id": 1, "method": "Runtime.evaluate", "params": {"expression": "a"}})
    assert player.exhausted is False
    player.send({"id": 2, "method": "Runtime.evaluate", "params": {"expression": "b"}})
    assert player.exhausted is True


def test_a_truncated_cassette_loses_only_its_last_frame(tmp_path):
    tape = tmp_path / "s.jsonl"
    _record(tape, lambda c: c.request("Runtime.evaluate", {"expression": "x"}))
    with tape.open("a") as fh:
        fh.write('{"t": "recv", "id": 9, "resu')       # killed mid-write
    player = Player(tape)
    player.send({"id": 1, "method": "Runtime.evaluate", "params": {"expression": "x"}})
    assert player.recv(timeout=1)["id"] == 1
