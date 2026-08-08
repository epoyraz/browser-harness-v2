"""Compose the 25 application recordings into one zoom-out 5x5 MP4 wall."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "top-25-applications-2026-08-08"
MANIFEST = OUT / "manifest.json"
VIDEO = OUT / "application-wall-5x5.mp4"
CONTACT_SHEET = OUT / ".application-wall-contact-sheet.png"

FPS = 30
CONTENT_SECONDS = 8.0
INTRO_SECONDS = 1.2
ZOOM_SECONDS = 2.5
TOTAL_SECONDS = INTRO_SECONDS + CONTENT_SECONDS
OUTPUT_W, OUTPUT_H = 3840, 2160
CELL_W, CELL_H = 768, 432
INNER_W, INNER_H = 764, 428
SCENE_CELL_W, SCENE_CELL_H = 1920, 1080
SCENE_INNER_W, SCENE_INNER_H = 1912, 1072


payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
values = sorted(
    (record["value"] for record in payload["records"] if record.get("ok")),
    key=lambda value: value["rank"],
)
if len(values) != 25:
    raise RuntimeError(f"expected 25 recordings, got {len(values)}")

# Build a high-resolution scene once. The camera moves over this 9600x5400 surface, so
# the opening cell is not a tiny grid tile enlarged fivefold.
scene_inputs: list[str] = []
scene_filters: list[str] = []
for index, value in enumerate(values):
    screenshot = ROOT / value["screenshot"]
    if not screenshot.is_file():
        raise FileNotFoundError(screenshot)
    scene_inputs.extend(["-i", str(screenshot)])
    scene_filters.append(
        f"[{index}:v]scale={SCENE_INNER_W}:{SCENE_INNER_H}:"
        "force_original_aspect_ratio=decrease,"
        f"pad={SCENE_CELL_W}:{SCENE_CELL_H}:(ow-iw)/2:(oh-ih)/2:color=white[s{index}]"
    )
scene_layout = "|".join(
    f"{column * SCENE_CELL_W}_{row * SCENE_CELL_H}"
    for row in range(5)
    for column in range(5)
)
scene_filters.append(
    f"{''.join(f'[s{index}]' for index in range(25))}"
    f"xstack=inputs=25:layout={scene_layout}:fill=white[scene]"
)
scene_result = subprocess.run(
    [
        "ffmpeg", "-v", "error", "-y", *scene_inputs,
        "-filter_complex", ";".join(scene_filters),
        "-map", "[scene]", "-frames:v", "1", "-update", "1", str(CONTACT_SHEET),
    ],
    capture_output=True,
    text=True,
    check=False,
)
if scene_result.returncode != 0 or not CONTACT_SHEET.is_file():
    raise RuntimeError(f"contact sheet failed: {scene_result.stderr[-1000:]}")

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

# The middle of a 5x5 grid is item 13 (zero-based index 12). The high-resolution scene
# carries its captured frame during the camera move; the live wall fades in after the
# camera arrives. Several recordings begin with a blank navigation frame, so embedding
# the live center clip during the intro would make an otherwise sharp opening turn white.
inputs.extend(["-loop", "1", "-framerate", str(FPS), "-i", str(CONTACT_SHEET)])

layout = "|".join(
    f"{column * CELL_W}_{row * CELL_H}"
    for row in range(5)
    for column in range(5)
)
stack_inputs = "".join(f"[v{index}]" for index in range(25))
filters.append(
    f"{stack_inputs}xstack=inputs=25:layout={layout}:fill=black:shortest=1[wall]"
)

filters.append(
    f"[25:v]fps={FPS},trim=duration={TOTAL_SECONDS},setpts=PTS-STARTPTS[scene-active]"
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
    f"d=1:s={OUTPUT_W}x{OUTPUT_H}:fps={FPS},settb=AVTB[camera]"
)
filters.append(
    f"[wall]fps={FPS},settb=AVTB[wall-ready]"
)
fade_start = INTRO_SECONDS + ZOOM_SECONDS - 0.3
filters.append(
    f"[camera][wall-ready]xfade=transition=fade:duration=0.3:offset={fade_start},"
    f"trim=duration={TOTAL_SECONDS},format=yuv420p[out]"
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
CONTACT_SHEET.unlink(missing_ok=True)

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
