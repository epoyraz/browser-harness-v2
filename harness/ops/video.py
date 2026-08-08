"""Recording → mp4 (v1 parity for the mechanical half).

**Deliberately only the mechanical half.** v1 spends ~1,270 lines on `video.py` +
`video_render.py`: an edit-brief schema, narration cadence validation, chapters, semantic
routes, a contact-sheet privacy pass with redaction rectangles, and an HTML compositor
driven through a browser. All of that is *editorial policy* — what to show, what to say,
what to hide — and policy is exactly what D6 says belongs in a skill rather than the
library. v1's own `make-video.md` is already written as one.

So this file answers the mechanical question only: turn the captured frames into a video
whose timing is the *real* timing, taken from the journal that recorded them. An honest
evidence artifact, not a cut. The editorial layer sits on top and can rewrite `plan.json`
without touching this code.

Timing is the part worth getting right. Frames are captured after each action, so a frame's
hold is the wall-clock gap to the next one — which shows a 4 s page load as 4 s. That is
faithful but tedious, so holds are clamped: nothing shorter than `MIN_HOLD` (or fast
actions flash past unreadably) and nothing longer than `MAX_HOLD` (or a video is mostly
waiting). Both are arguments; the clamping is reported so a viewer knows time was
compressed.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from harness.core import jsonl

MIN_HOLD = 0.6
MAX_HOLD = 3.0


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffmpeg"))


def _entries(recording: Path) -> list[dict[str, Any]]:
    journal = recording / "session.jsonl"
    return [entry for entry in jsonl.read(journal)
            if entry.get("kind") == "call" and entry.get("frame")]


def plan(recording: Path, *, min_hold: float = MIN_HOLD,
         max_hold: float = MAX_HOLD) -> dict[str, Any]:
    """Frames with real holds, in capture order. The editorial layer edits *this*."""
    recording = Path(recording)
    entries = _entries(recording)
    shots: list[dict[str, Any]] = []
    for i, e in enumerate(entries):
        frame = recording / str(e["frame"])
        if not frame.is_file():
            continue
        # Wall-clock to the next captured action; the last frame gets its own duration.
        if i + 1 < len(entries):
            gap = float(entries[i + 1].get("ts", 0)) - float(e.get("ts", 0))
        else:
            gap = float(e.get("ms", 0)) / 1000.0
        hold = max(min_hold, min(max_hold, gap if gap > 0 else min_hold))
        shots.append({
            "frame": frame.name, "fn": e.get("fn"), "hold": round(hold, 3),
            "real": round(max(gap, 0.0), 3), "clamped": round(hold, 3) != round(gap, 3),
            "url": e.get("url"), "title": e.get("title"),
            "ok": bool((e.get("outcome") or {}).get("ok", True)),
            "outcome_class": (e.get("outcome") or {}).get("class"),
        })
    meta = {}
    try:
        meta = json.loads((recording / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {"recording": str(recording), "title": meta.get("title") or meta.get("name"),
            "shots": shots, "duration": round(sum(s["hold"] for s in shots), 2),
            "real_duration": round(sum(s["real"] for s in shots), 2)}


def export(recording: str | Path, output: str | Path | None = None, *,
           fps: int = 30, width: int | None = None,
           overwrite: bool = False) -> dict[str, Any]:
    """Render the plan to mp4 via ffmpeg's concat demuxer.

    Concat with per-frame durations rather than a fixed frame rate: the holds are real
    measurements, and re-sampling them to a constant rate would throw away the one thing
    that makes this evidence rather than a slideshow.
    """
    recording = Path(recording)
    if not recording.is_dir():
        raise FileNotFoundError(f"no such recording: {recording}")
    if not have_ffmpeg():
        raise RuntimeError("ffmpeg is required to export a video (brew install ffmpeg)")
    p = plan(recording)
    if not p["shots"]:
        raise ValueError(f"{recording} has no frames to export — was it recorded with "
                         f"BH_RECORD=1?")
    out = Path(output) if output else recording / "video.mp4"
    if out.exists() and not overwrite:
        raise FileExistsError(f"{out} exists — pass a different --output, or --overwrite")

    # concat demuxer: each entry repeats the file, then a final bare `file` line, because
    # the last duration is otherwise ignored.
    lines = []
    for s in p["shots"]:
        lines.append(f"file '{s['frame']}'")
        lines.append(f"duration {s['hold']}")
    lines.append(f"file '{p['shots'][-1]['frame']}'")
    listing = recording / ".concat.txt"
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Even dimensions and an EXPLICIT -pix_fmt: JPEG input carries yuvj420p (full-range),
    # and a `format=yuv420p` filter alone leaves that tag in place — QuickTime and most
    # browsers want plain yuv420p, and ffmpeg reports success either way.
    base = f"scale={width}:-2" if width else "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    # out_range=tv: JPEG frames are full-range, and libx264 keeps that tag
    # (reported as yuvj420p) unless the conversion is asked for explicitly.
    scale = f"{base}:out_range=tv,format=yuv420p"
    cmd = ["ffmpeg", "-v", "error", "-y" if overwrite else "-n",
           "-f", "concat", "-safe", "0", "-i", str(listing),
           "-vf", scale, "-pix_fmt", "yuv420p", "-r", str(fps),
           "-c:v", "libx264", "-preset", "medium", "-crf", "23",
           "-movflags", "+faststart", str(out)]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False,
                          cwd=str(recording))
    listing.unlink(missing_ok=True)
    if proc.returncode != 0 or not out.is_file():
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.strip()[:400]}")
    (recording / "plan.json").write_text(json.dumps(p, indent=2), encoding="utf-8")
    return {"path": str(out), "bytes": out.stat().st_size, "shots": len(p["shots"]),
            "duration": p["duration"], "real_duration": p["real_duration"],
            "clamped": sum(1 for s in p["shots"] if s["clamped"])}
