"""Frames -> mp4. Timing comes from the journal, so the video is evidence not a slideshow."""
import json

import pytest

from harness.ops import video
from harness.ops.video import MAX_HOLD, MIN_HOLD, export, have_ffmpeg, plan


def _rec(tmp_path, entries, frames=3):
    (tmp_path / "meta.json").write_text(json.dumps({"name": "r", "title": "T"}))
    (tmp_path / "session.jsonl").write_text(
        "\n".join(json.dumps(e) for e in entries) + "\n")
    for i in range(1, frames + 1):
        (tmp_path / f"{i:04d}.jpg").write_bytes(b"x")
    return tmp_path


def test_holds_are_the_real_gaps_between_captures(tmp_path):
    rec = _rec(tmp_path, [
        {"kind": "call", "fn": "goto", "ts": 100.0, "frame": "0001.jpg", "ms": 50},
        {"kind": "call", "fn": "click", "ts": 102.0, "frame": "0002.jpg", "ms": 50},
        {"kind": "call", "fn": "scroll", "ts": 102.9, "frame": "0003.jpg", "ms": 400},
    ])
    shots = plan(rec)["shots"]
    assert [s["real"] for s in shots[:2]] == [2.0, 0.9]     # measured, not assumed
    assert shots[0]["hold"] == 2.0


def test_holds_are_clamped_at_both_ends_and_say_so(tmp_path):
    """Faithful timing shows a 40s load as 40s; unclamped it is mostly waiting."""
    rec = _rec(tmp_path, [
        {"kind": "call", "fn": "goto", "ts": 0.0, "frame": "0001.jpg", "ms": 1},
        {"kind": "call", "fn": "click", "ts": 40.0, "frame": "0002.jpg", "ms": 1},
        {"kind": "call", "fn": "click", "ts": 40.01, "frame": "0003.jpg", "ms": 1},
    ])
    shots = plan(rec)["shots"]
    assert shots[0]["hold"] == MAX_HOLD and shots[0]["clamped"]
    assert shots[1]["hold"] == MIN_HOLD and shots[1]["clamped"]
    assert shots[0]["real"] == 40.0                        # the truth is still recorded


def test_only_entries_that_actually_produced_a_frame_are_shots(tmp_path):
    rec = _rec(tmp_path, [
        {"kind": "call", "fn": "snapshot", "ts": 1.0},                    # no frame
        {"kind": "call", "fn": "goto", "ts": 2.0, "frame": "0001.jpg"},
        {"kind": "call", "fn": "goto", "ts": 3.0, "frame": "9999.jpg"},   # frame missing
        {"kind": "note", "msg": "x"},
    ])
    assert [s["frame"] for s in plan(rec)["shots"]] == ["0001.jpg"]


def test_a_failed_action_keeps_its_class_for_the_editorial_layer(tmp_path):
    rec = _rec(tmp_path, [{"kind": "call", "fn": "goto", "ts": 1.0, "frame": "0001.jpg",
                           "outcome": {"ok": False, "class": "navigation_failed"}}])
    s = plan(rec)["shots"][0]
    assert s["ok"] is False and s["outcome_class"] == "navigation_failed"


def test_exporting_an_empty_recording_says_why(tmp_path):
    rec = _rec(tmp_path, [{"kind": "call", "fn": "snapshot", "ts": 1.0}], frames=0)
    with pytest.raises(ValueError, match="BH_RECORD"):
        export(rec)


def test_a_missing_recording_is_not_a_confusing_ffmpeg_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        export(tmp_path / "nope")


def test_export_routes_continuous_frames_to_the_screencast_encoder(
        tmp_path, monkeypatch):
    (tmp_path / "frames.jsonl").write_text("{}\n")
    monkeypatch.setattr(video, "have_ffmpeg", lambda: True)
    seen = {}

    def fake(recording, output, **options):
        seen.update(recording=recording, output=output, options=options)
        return {"mode": "cdp_screencast"}

    monkeypatch.setattr(video, "export_screencast", fake)
    assert export(tmp_path, "movie.mp4")["mode"] == "cdp_screencast"
    assert seen["recording"] == tmp_path.resolve()


@pytest.mark.skipif(not have_ffmpeg(), reason="ffmpeg not installed")
def test_export_refuses_to_clobber_an_existing_cut(tmp_path):
    rec = _rec(tmp_path, [{"kind": "call", "fn": "goto", "ts": 1.0, "frame": "0001.jpg"}])
    (rec / "video.mp4").write_bytes(b"old")
    with pytest.raises(FileExistsError):
        export(rec)
