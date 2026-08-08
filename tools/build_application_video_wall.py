"""Compose the 25 application recordings into one zoom-out 5x5 MP4 wall."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "top-25-applications-2026-08-08"
MANIFEST = OUT / "manifest.json"
VIDEO = OUT / "application-wall-5x5.mp4"

FPS = 30
CONTENT_SECONDS = 8.0
INTRO_SECONDS = 1.2
ZOOM_SECONDS = 2.5
TOTAL_SECONDS = INTRO_SECONDS + CONTENT_SECONDS
CELL_W, CELL_H = 384, 216
INNER_W, INNER_H = 380, 212


payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
values = sorted(
    (record["value"] for record in payload["records"] if record.get("ok")),
    key=lambda value: value["rank"],
)
if len(values) != 25:
    raise RuntimeError(f"expected 25 recordings, got {len(values)}")

inputs: list[str] = []
filters: list[str] = []
for index, value in enumerate(values):
    path = OUT / value["video"]
    if not path.is_file():
        raise FileNotFoundError(path)
    duration = float(value["recording_seconds"])
    stretch = CONTENT_SECONDS / duration
    inputs.extend(["-i", str(path)])
    filters.append(
        f"[{index}:v]setpts=(PTS-STARTPTS)*{stretch:.8f},fps={FPS},"
        f"scale={INNER_W}:{INNER_H}:force_original_aspect_ratio=decrease,"
        f"pad={CELL_W}:{CELL_H}:(ow-iw)/2:(oh-ih)/2:color=white,"
        f"tpad=start_mode=clone:start_duration={INTRO_SECONDS},"
        f"trim=duration={TOTAL_SECONDS}[v{index}]"
    )

layout = "|".join(
    f"{column * CELL_W}_{row * CELL_H}"
    for row in range(5)
    for column in range(5)
)
stack_inputs = "".join(f"[v{index}]" for index in range(25))
filters.append(
    f"{stack_inputs}xstack=inputs=25:layout={layout}:fill=black:shortest=1[wall]"
)

hold_frames = round(INTRO_SECONDS * FPS)
zoom_frames = round(ZOOM_SECONDS * FPS)
zoom_end = hold_frames + zoom_frames
zoom = (
    f"if(lte(on,{hold_frames - 1}),5,"
    f"if(lte(on,{zoom_end}),5-4*(on-{hold_frames - 1})/{zoom_frames},1))"
)
filters.append(
    f"[wall]zoompan=z='{zoom}':x=0:y=0:d=1:s=1920x1080:fps={FPS},"
    "format=yuv420p[out]"
)

command = [
    "ffmpeg", "-v", "error", "-y", *inputs,
    "-filter_complex", ";".join(filters),
    "-map", "[out]", "-an", "-t", str(TOTAL_SECONDS),
    "-c:v", "libx264", "-preset", "medium", "-crf", "21",
    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(VIDEO),
]
result = subprocess.run(command, capture_output=True, text=True, check=False)
if result.returncode != 0 or not VIDEO.is_file():
    raise RuntimeError(f"ffmpeg failed: {result.stderr[-1000:]}")

probe = subprocess.run(
    [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate:format=duration,size",
        "-of", "json", str(VIDEO),
    ],
    capture_output=True,
    text=True,
    check=True,
)
print(json.dumps({"video": str(VIDEO), **json.loads(probe.stdout)}, indent=2))
