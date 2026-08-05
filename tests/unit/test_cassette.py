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


# --- size -------------------------------------------------------------------

def test_bulky_payloads_are_stored_as_a_digest(tmp_path):
    """One screenshot response was 51 KB of a 54 KB session; a digest still compares equal
    across runs, which is all replay needs."""
    tape = tmp_path / "s.jsonl"
    browser = FakeBrowser("a")
    recorder = Recorder(browser, tape)
    recorder.send({"id": 1, "method": "Page.captureScreenshot",
                   "params": {"data": "Q" * (ELIDE_OVER + 1)}})
    frames = [json.loads(x) for x in tape.read_text().splitlines()]
    assert frames[0]["params"]["data"]["_elided"] == ELIDE_OVER + 1
    assert len(tape.read_bytes()) < 400


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
