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
OUTPUT_W, OUTPUT_H = 3840, 2160
SCENE_CELL_W, SCENE_CELL_H = 960, 540
SCENE_INNER_W, SCENE_INNER_H = 952, 532


payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
values = sorted(
    (record["value"] for record in payload["records"] if record.get("ok")),
    key=lambda value: value["rank"],
)
if len(values) != 25:
    raise RuntimeError(f"expected 25 recordings, got {len(values)}")

# Put a genuinely changing recording in the center. Rivia's surviving content is one
# repeated state; Wavestone visibly advances from its job page to its application UI.
center_index = 12
wavestone_index = next(
    index for index, value in enumerate(values)
    if value["rank"] == 42 and value["company"] == "Wavestone"
)
values[center_index], values[wavestone_index] = values[wavestone_index], values[center_index]

center = values[center_index]
center_video = OUT / center["video"]
center_probe = subprocess.run(
    [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(center_video),
    ],
    capture_output=True,
    text=True,
    check=True,
)
# The first Wavestone state remains visible through 2.5s. Starting at 1.5s retains that
# opening and places its transition to the application UI at the end of the camera move.
center_trim = 1.5
center_duration = float(center_probe.stdout.strip()) - center_trim
if center_duration <= 0:
    raise RuntimeError("center recording has no content after its blank prefix")

inputs: list[str] = []
filters: list[str] = []
for index, value in enumerate(values):
    path = OUT / value["video"]
    if not path.is_file():
        raise FileNotFoundError(path)
    duration = float(value["recording_seconds"])
    stretch = CONTENT_SECONDS / (center_duration if index == center_index else duration)
    inputs.extend(["-i", str(path)])
    trim = f"trim=start={center_trim}," if index == center_index else ""
    normalized = (
        f"[{index}:v]{trim}setpts=(PTS-STARTPTS)*{stretch:.8f},fps={FPS},"
        f"tpad=start_mode=clone:start_duration={INTRO_SECONDS},"
        f"trim=duration={TOTAL_SECONDS}"
    )
    filters.append(
        f"{normalized},scale={SCENE_INNER_W}:{SCENE_INNER_H}:"
        "force_original_aspect_ratio=decrease,"
        f"pad={SCENE_CELL_W}:{SCENE_CELL_H}:(ow-iw)/2:(oh-ih)/2:color=white[v{index}]"
    )

layout = "|".join(
    f"{column * SCENE_CELL_W}_{row * SCENE_CELL_H}"
    for row in range(5)
    for column in range(5)
)
stack_inputs = "".join(f"[v{index}]" for index in range(25))
filters.append(
    f"{stack_inputs}xstack=inputs=25:layout={layout}:fill=black:shortest=1[scene-active]"
)

hold_frames = round(INTRO_SECONDS * FPS)
zoom_frames = round(ZOOM_SECONDS * FPS)
zoom_end = hold_frames + zoom_frames
camera_zoom = (
    f"if(lte(on,{hold_frames - 1}),5,"
    f"if(lte(on,{zoom_end}),5-4*(on-{hold_frames - 1})/{zoom_frames},1))"
)
filters.append(
    f"[scene-active]zoompan=z='{camera_zoom}':"
    "x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':"
    f"d=1:s={OUTPUT_W}x{OUTPUT_H}:fps={FPS},format=yuv420p[out]"
)

command = [
    "ffmpeg", "-v", "error", "-y", *inputs,
    "-filter_complex", ";".join(filters),
    "-map", "[out]", "-an", "-t", str(TOTAL_SECONDS),
    "-c:v", "libx264", "-preset", "medium", "-crf", "19",
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
