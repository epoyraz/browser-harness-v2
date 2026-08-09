import base64
import json
import time

import pytest

from harness.ops.screencast import ScreencastRecorder

JPEG = base64.b64encode(b"jpeg-frame").decode()


class _Connection:
    def __init__(self):
        self.listeners = []

    def subscribe(self, fn):
        self.listeners.append(fn)

    def unsubscribe(self, fn):
        self.listeners.remove(fn)


class _Tab:
    def __init__(self):
        self._conn = _Connection()
        self.calls = []

    def _sid(self):
        return "session-1"

    def cdp(self, method, params=None, **_kw):
        self.calls.append((method, params or {}))
        return {}


def test_screencast_acks_and_persists_timestamped_frames(tmp_path):
    tab = _Tab()
    recorder = ScreencastRecorder(tab, tmp_path, quality=90)
    tab._conn.listeners[0]({
        "method": "Page.screencastFrame", "sessionId": "session-1",
        "params": {"sessionId": 7, "data": JPEG,
                   "metadata": {"timestamp": 123.5, "scrollOffsetY": 42}},
    })
    deadline = time.time() + 2
    while recorder.frames < 1 and time.time() < deadline:
        time.sleep(0.01)
    recorder.stop()
    assert (tmp_path / "frames/000001.jpg").read_bytes() == b"jpeg-frame"
    row = json.loads((tmp_path / "frames.jsonl").read_text())
    assert row["timestamp"] == 123.5 and row["scroll_y"] == 42
    assert ("Page.screencastFrameAck", {"sessionId": 7}) in tab.calls
    assert tab.calls[0][0] == "Page.startScreencast"
    assert tab.calls[-1][0] == "Page.stopScreencast"
    assert json.loads((tmp_path / "meta.json").read_text())["active"] is False


def test_screencast_rejects_invalid_quality(tmp_path):
    with pytest.raises(ValueError, match="quality"):
        ScreencastRecorder(_Tab(), tmp_path, quality=101)


def test_screencast_ignores_other_sessions(tmp_path):
    tab = _Tab(); recorder = ScreencastRecorder(tab, tmp_path)
    tab._conn.listeners[0]({"method": "Page.screencastFrame", "sessionId": "other",
                            "params": {"sessionId": 9, "data": JPEG}})
    recorder.stop()
    assert recorder.frames == 0
