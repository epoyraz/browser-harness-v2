"""Continuous compositor recording through CDP ``Page.startScreencast``.

Unlike the action recorder, this captures frames whenever Chrome composites them.  CDP
still transports JPEG/PNG images rather than a video bitstream, so frames are acknowledged
off the browser event thread, persisted with their own timestamps, and encoded afterwards.
"""
from __future__ import annotations

import base64
import json
import queue
import threading
import time
from pathlib import Path
from typing import Any

from evidence.record import recordings_root
from harness.core.outcome import HarnessError


class ScreencastRecorder:
    def __init__(self, tab: Any, directory: Path, *, quality: int = 88,
                 max_width: int = 1440, max_height: int = 1000,
                 every_nth_frame: int = 1):
        if not 0 <= quality <= 100:
            raise ValueError("screencast quality must be between 0 and 100")
        if min(max_width, max_height, every_nth_frame) < 1:
            raise ValueError("screencast dimensions and frame interval must be positive")
        self.tab, self.dir = tab, directory
        self.frames_dir = directory / "frames"
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.frames_path = directory / "frames.jsonl"
        self.quality, self.max_width, self.max_height = quality, max_width, max_height
        self.every_nth_frame = every_nth_frame
        self.frames = 0
        self.dropped = 0
        self.started = time.time()
        self.stopped: float | None = None
        self._sid = tab._sid()
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=256)
        self._acks: queue.SimpleQueue[int | None] = queue.SimpleQueue()
        self._listener = self._on_event
        self._thread = threading.Thread(target=self._write_frames,
                                        name="bh-screencast", daemon=True)
        self._ack_thread = threading.Thread(target=self._ack_frames,
                                            name="bh-screencast-ack", daemon=True)
        self._write_meta(active=True)
        tab._conn.subscribe(self._listener)
        self._ack_thread.start()
        self._thread.start()
        try:
            tab.cdp("Page.startScreencast", {
                "format": "jpeg", "quality": quality, "maxWidth": max_width,
                "maxHeight": max_height, "everyNthFrame": every_nth_frame,
            })
        except Exception:
            tab._conn.unsubscribe(self._listener)
            self._acks.put(None)
            self._queue.put(None)
            self._ack_thread.join(2)
            self._thread.join(2)
            raise

    def _write_meta(self, *, active: bool) -> None:
        payload = {
            "mode": "cdp_screencast", "started": round(self.started, 3),
            "stopped": round(self.stopped, 3) if self.stopped else None,
            "active": active, "format": "jpeg", "quality": self.quality,
            "max_width": self.max_width, "max_height": self.max_height,
            "every_nth_frame": self.every_nth_frame, "frames": self.frames,
            "dropped": self.dropped,
        }
        (self.dir / "meta.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _on_event(self, message: dict[str, Any]) -> None:
        if message.get("method") != "Page.screencastFrame":
            return
        if message.get("sessionId") not in (None, self._sid):
            return
        frame_session = (message.get("params") or {}).get("sessionId")
        if frame_session is not None:
            # ACKs have their own unbounded control queue. The data queue is deliberately
            # bounded, but even a discarded frame must be acknowledged or Chrome stops
            # delivering the entire screencast.
            self._acks.put(frame_session)
        try:
            self._queue.put_nowait(message)
        except queue.Full:
            self.dropped += 1

    def _ack_frames(self) -> None:
        while (session_id := self._acks.get()) is not None:
            try:
                # Never request on the event pump thread: waiting for this reply there
                # would deadlock the only reader that can receive it.
                self.tab.cdp("Page.screencastFrameAck", {"sessionId": session_id},
                             timeout=5.0)
            except HarnessError:
                self.dropped += 1

    def _write_frames(self) -> None:
        while (message := self._queue.get()) is not None:
            params = message.get("params") or {}
            try:
                data = base64.b64decode(params.get("data") or "", validate=True)
            except (ValueError, TypeError):
                self.dropped += 1
                continue
            if not data:
                self.dropped += 1
                continue
            self.frames += 1
            name = f"frames/{self.frames:06d}.jpg"
            (self.dir / name).write_bytes(data)
            metadata = params.get("metadata") or {}
            entry = {
                "frame": name, "captured": round(time.time(), 6),
                "timestamp": metadata.get("timestamp"),
                "scroll_x": metadata.get("scrollOffsetX"),
                "scroll_y": metadata.get("scrollOffsetY"),
                "page_scale": metadata.get("pageScaleFactor"),
                "device_width": metadata.get("deviceWidth"),
                "device_height": metadata.get("deviceHeight"),
            }
            with self.frames_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, separators=(",", ":")) + "\n")

    def stop(self) -> Path:
        if self.stopped is not None:
            return self.dir
        try:
            self.tab.cdp("Page.stopScreencast", timeout=10.0)
        finally:
            self.tab._conn.unsubscribe(self._listener)
            self._acks.put(None)
            self._queue.put(None)
            self._ack_thread.join(10)
            self._thread.join(10)
            self.stopped = time.time()
            self._write_meta(active=False)
        return self.dir


def start(tab: Any, *, name: str | None = None, quality: int = 88,
          max_width: int = 1440, max_height: int = 1000,
          every_nth_frame: int = 1) -> ScreencastRecorder:
    name = name or time.strftime("screencast-%Y%m%d-%H%M%S")
    directory = recordings_root() / name
    directory.mkdir(parents=True, exist_ok=True)
    return ScreencastRecorder(tab, directory, quality=quality,
                              max_width=max_width, max_height=max_height,
                              every_nth_frame=every_nth_frame)
