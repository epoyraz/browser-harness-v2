"""Recording and screencast, tested where they now live.

Moved from `test_session.py`. They are functions over a session rather than methods on it,
so `s.start_recording()` reads `recording.start_recording(s)`; the lifetime still ends with
the session, now through the `at_close` hook the harness offers without knowing what is
registered on it.
"""
import json
from pathlib import Path

import pytest

from evidence import recording
from harness.session import Session


def test_continuous_screencast_is_exposed_and_stopped_with_the_session(
        session, monkeypatch, tmp_path):
    class Recorder:
        dir = tmp_path

        def __init__(self):
            self.stops = 0

        def stop(self):
            self.stops += 1
            return self.dir

    recorder = Recorder()
    monkeypatch.setattr("evidence.screencast.start", lambda *_a, **_kw: recorder)
    # The surface is bound by the layer that owns it, not by the harness.
    ns = session.namespace()
    recording.install(ns)
    assert ns["start_screencast"]() == str(tmp_path)
    assert ns["stop_screencast"]() == str(tmp_path)
    assert recorder.stops == 1


def test_recording_profile_is_public_persisted_and_cannot_change_midstream(
        session, monkeypatch, tmp_path):
    monkeypatch.setenv("BH_RECORDINGS", str(tmp_path))
    directory = recording.start_recording(session, name="proof", profile="evidence")
    meta = json.loads((Path(directory) / "meta.json").read_text())
    assert meta["recording_profile"] == "evidence"
    assert recording._recorders[id(session)].profile.value == "evidence"
    with pytest.raises(ValueError, match="already uses profile"):
        recording.start_recording(session, profile="cinematic")
    assert recording.stop_recording(session) == directory


def test_recording_profile_environment_applies_to_manual_start(session, monkeypatch, tmp_path):
    monkeypatch.setenv("BH_RECORDINGS", str(tmp_path))
    monkeypatch.setenv("BH_RECORD_PROFILE", "cinematic")
    recording.start_recording(session, name="film")
    assert recording._recorders[id(session)].profile.value == "cinematic"


def test_bh_record_may_name_the_profile_directly(served, monkeypatch, tmp_path):
    monkeypatch.setenv("BH_RECORDINGS", str(tmp_path))
    monkeypatch.setenv("BH_RECORD", "evidence")
    automatic = Session("sesstest")
    try:
        # `BH_RECORD` is honoured when the layer installs itself; `Session` used to read
        # the variable directly, which meant a harness that knew what recording was.
        recording.install(automatic.namespace())
        assert recording._recorders.get(id(automatic)) is not None
        assert recording._recorders[id(automatic)].profile.value == "evidence"
    finally:
        automatic.close()

