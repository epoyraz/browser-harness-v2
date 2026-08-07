"""Recording hangs off the journal — no second capture path, no parallel events file."""
import json
import threading

import pytest

from harness.core.journal import Journal
from harness.ops.record import ACTIONS, scrub, start


class _FakeTab:
    def __init__(self):
        self.shots = []

    def capture_screenshot(self, path, **kw):
        self.shots.append(str(path))
        __import__("pathlib").Path(path).write_bytes(b"jpeg")
        return {"bytes": 4}

    def js(self, expr, **kw):
        return {"u": "https://x.test/cb?code=SECRET&a=1", "t": "Title",
                "box": [1.4, 2.6, 30.2, 10.0]}


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_RECORDINGS", str(tmp_path))
    j = Journal(tmp_path / "pre.jsonl", session="s")
    tab = _FakeTab()
    rec = start(lambda: tab, j, name="r1", title="T")
    return j, tab, rec


def test_a_frame_lands_on_the_call_entry_itself(wired):
    """v1 keeps a parallel events.jsonl duplicating its trace; here the frame is a field
    on the call that produced it, so `bh trace` renders a recording unchanged."""
    j, _tab, rec = wired
    with j.call("goto", url="https://x.test/"):
        pass
    entry = next(e for e in j.entries() if e["kind"] == "call")
    assert entry["frame"] == "0001.jpg" and entry["fn"] == "goto"
    assert not (rec.dir / "events.jsonl").exists()
    assert (rec.dir / "0001.jpg").is_file()


def test_only_state_changing_calls_get_a_frame(wired):
    """Read-only calls would fill an inspection-heavy session with identical images."""
    j, tab, _ = wired
    for fn in ("snapshot", "page_text", "form_schema", "js"):
        with j.call(fn):
            pass
    assert tab.shots == []
    with j.call("click"):
        pass
    assert len(tab.shots) == 1


def test_the_capture_cannot_recurse_into_itself(wired):
    """The screenshot opens spans of its own; without the guard the hook fires on its own
    frame until the stack gives out."""
    j, tab, rec = wired

    def nested(path, **kw):
        with j.call("goto"):          # the capture's own inner span
            pass
        return {"bytes": 0}
    tab.capture_screenshot = nested
    with j.call("goto"):
        pass
    assert rec.frames == 1


def test_parallel_actions_capture_each_worker_tab_once(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_RECORDINGS", str(tmp_path))
    monkeypatch.setattr("harness.ops.record.SETTLE", 0)
    journal = Journal(tmp_path / "pre.jsonl", session="s")
    local = threading.local()
    tabs = [_FakeTab() for _ in range(6)]
    recorder = start(lambda: local.tab, journal, name="parallel")
    barrier = threading.Barrier(len(tabs), timeout=5)

    def action(tab):
        local.tab = tab
        barrier.wait()
        with journal.call("goto"):
            pass

    threads = [threading.Thread(target=action, args=(tab,)) for tab in tabs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert recorder.frames == len(tabs)
    assert all(len(tab.shots) == 1 for tab in tabs)
    assert sorted(path.name for path in recorder.dir.glob("*.jpg")) == [
        f"{index:04d}.jpg" for index in range(1, len(tabs) + 1)]


def test_url_secrets_are_scrubbed_before_they_reach_disk(wired):
    j, _, _ = wired
    with j.call("goto"):
        pass
    entry = next(e for e in j.entries() if e["kind"] == "call")
    assert "SECRET" not in json.dumps(entry) and "REDACTED" in entry["url"]


def test_scrub_covers_the_oauth_families():
    for p in ("code", "access_token", "id_token", "client_secret", "api_key", "password"):
        assert "REDACTED" in scrub(f"https://x/cb?{p}=abc123")
    assert scrub("https://x/?q=hello") == "https://x/?q=hello"


def test_a_capture_failure_never_breaks_the_run(wired):
    j, tab, _ = wired

    def boom(path, **kw):
        raise RuntimeError("no renderer")
    tab.capture_screenshot = boom
    with j.call("goto"):
        pass                            # must not raise
    assert next(e for e in j.entries() if e["kind"] == "call").get("frame") is None


def test_stopping_detaches_the_hook(wired):
    j, tab, rec = wired
    rec.stop()
    with j.call("goto"):
        pass
    assert tab.shots == []


def test_the_journal_moves_into_the_recording(wired):
    j, _, rec = wired
    assert j.path == rec.dir / "session.jsonl"
    assert json.loads((rec.dir / "meta.json").read_text())["title"] == "T"


def test_the_allowlist_is_state_changing_calls_only():
    assert {"goto", "click", "fill_form", "scroll", "press_key"} <= ACTIONS
    assert not ({"snapshot", "see", "page_text", "js", "form_schema"} & ACTIONS)
