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
CELL_W, CELL_H = 768, 432
INNER_W, INNER_H = 764, 428


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

# The middle of a 5x5 grid is item 13 (zero-based index 12). Its full-resolution PNG
# supplies the sharp opening frame; shrinking a 768x432 tile back to 4K would recreate
# the exact quality loss this compositor exists to avoid.
center = values[12]
center_screenshot = ROOT / center["screenshot"]
if not center_screenshot.is_file():
    raise FileNotFoundError(center_screenshot)
inputs.extend(["-loop", "1", "-framerate", str(FPS), "-i", str(center_screenshot)])

layout = "|".join(
    f"{column * CELL_W}_{row * CELL_H}"
    for row in range(5)
    for column in range(5)
)
stack_inputs = "".join(f"[v{index}]" for index in range(25))
filters.append(
    f"{stack_inputs}xstack=inputs=25:layout={layout}:fill=black:shortest=1[wall]"
)

focus_w = (
    f"if(lte(t,{INTRO_SECONDS}),{OUTPUT_W},"
    f"if(lte(t,{INTRO_SECONDS + ZOOM_SECONDS}),"
    f"{OUTPUT_W}-({OUTPUT_W}-{INNER_W})*(t-{INTRO_SECONDS})/{ZOOM_SECONDS},{INNER_W}))"
)
focus_h = (
    f"if(lte(t,{INTRO_SECONDS}),{OUTPUT_H},"
    f"if(lte(t,{INTRO_SECONDS + ZOOM_SECONDS}),"
    f"{OUTPUT_H}-({OUTPUT_H}-{INNER_H})*(t-{INTRO_SECONDS})/{ZOOM_SECONDS},{INNER_H}))"
)
target_x = 2 * CELL_W + (CELL_W - INNER_W) // 2
target_y = 2 * CELL_H + (CELL_H - INNER_H) // 2
focus_x = (
    f"if(lte(t,{INTRO_SECONDS}),0,"
    f"if(lte(t,{INTRO_SECONDS + ZOOM_SECONDS}),"
    f"{target_x}*(t-{INTRO_SECONDS})/{ZOOM_SECONDS},{target_x}))"
)
focus_y = (
    f"if(lte(t,{INTRO_SECONDS}),0,"
    f"if(lte(t,{INTRO_SECONDS + ZOOM_SECONDS}),"
    f"{target_y}*(t-{INTRO_SECONDS})/{ZOOM_SECONDS},{target_y}))"
)
fade_start = INTRO_SECONDS + ZOOM_SECONDS - 0.3
filters.append(
    f"[25:v]scale={OUTPUT_W}:{OUTPUT_H}:force_original_aspect_ratio=decrease,"
    f"pad={OUTPUT_W}:{OUTPUT_H}:(ow-iw)/2:(oh-ih)/2:color=white,"
    f"trim=duration={TOTAL_SECONDS},setpts=PTS-STARTPTS,format=rgba,"
    f"scale=w='{focus_w}':h='{focus_h}':eval=frame,"
    f"fade=t=out:st={fade_start}:d=0.3:alpha=1[focus]"
)
filters.append(
    f"[wall][focus]overlay=x='{focus_x}':y='{focus_y}':eval=frame:shortest=1,"
    "format=yuv420p[out]"
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
