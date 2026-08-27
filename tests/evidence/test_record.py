"""Recording hangs off the journal — no second capture path, no parallel events file."""
import json
import threading

import pytest

from evidence.record import ACTIONS, ALREADY_SETTLED, Profile, parse_profile, scrub, start
from harness.core.journal import Journal


class _FakeTab:
    def __init__(self):
        self.target_id = "target-a"
        self.shots = []

    def capture_screenshot(self, path, **kw):
        self.shots.append(str(path))
        __import__("pathlib").Path(path).write_bytes(b"jpeg")
        return {"bytes": 4, "cdp_calls": 2, "context": {
            "u": "https://x.test/cb?code=SECRET&a=1", "t": "Title",
            "box": [1.4, 2.6, 30.2, 10.0]}}

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
    assert entry["frame_span_id"] == entry["id"]
    assert entry["frame_target_id"] == "target-a"
    assert entry["frame_cdp"] == 2 and entry["frame_bytes"] == 4
    assert entry["frame_screenshot_ms"] >= 0 and entry["frame_recording_ms"] >= 0
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
    monkeypatch.setattr("evidence.record.SETTLE", 0)
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
    entry = next(e for e in j.entries() if e["kind"] == "call")
    assert entry.get("frame") is None
    assert entry["frame_suppressed"] == "capture_failed"
    assert entry["error_class"] == "RuntimeError"


def test_stopping_detaches_the_hook(wired):
    j, tab, rec = wired
    rec.stop()
    with j.call("goto"):
        pass
    assert tab.shots == []


def test_the_journal_moves_into_the_recording(wired):
    j, _, rec = wired
    assert j.path == rec.dir / "session.jsonl"
    meta = json.loads((rec.dir / "meta.json").read_text())
    assert meta["title"] == "T" and meta["recording_profile"] == "review"


@pytest.mark.parametrize(
    ("profile", "frames", "suppressed"),
    [
        ("evidence", 2, {"nested_consequence": 2}),
        ("review", 3, {"profile_policy": 1}),
        ("cinematic", 4, {}),
    ],
)
def test_fixed_workflow_has_deterministic_profile_frame_manifest(
        tmp_path, monkeypatch, profile, frames, suppressed):
    """One navigation plus one two-click semantic selection is the fixed manifest.

    Evidence keeps the selection's final state, review retains its two diagnostic clicks,
    and cinematic retains both visual beats plus the high-level completed selection.
    """
    monkeypatch.setenv("BH_RECORDINGS", str(tmp_path))
    monkeypatch.setattr("evidence.record.SETTLE", 0)
    journal = Journal(tmp_path / "pre.jsonl", session="s")
    tab = _FakeTab()
    recorder = start(lambda: tab, journal, name=profile, profile=profile)

    with journal.call("goto"):
        pass
    with journal.call("select_option"):
        with journal.call("click"):
            pass
        with journal.call("click"):
            pass
    recorder.stop()

    entries = [entry for entry in journal.entries() if entry.get("kind") == "call"]
    retained = [entry for entry in entries if entry.get("frame")]
    reasons = {}
    for entry in entries:
        if reason := entry.get("frame_suppressed"):
            reasons[reason] = reasons.get(reason, 0) + 1
    assert len(retained) == frames
    assert reasons == suppressed
    assert all(entry["frame_span_id"] == entry["id"] for entry in retained)
    assert all(entry["frame_target_id"] == tab.target_id for entry in retained)
    assert len(list(recorder.dir.glob("*.jpg"))) == frames
    summary = next(entry for entry in journal.entries()
                   if entry.get("event") == "recording_summary")
    assert summary["recording_profile"] == profile
    assert summary["frames"] == frames and summary["frame_suppressed"] == suppressed


def test_profile_names_are_explicit_and_typos_fail_closed():
    assert [profile.value for profile in Profile] == ["evidence", "review", "cinematic"]
    assert parse_profile(None) is Profile.REVIEW
    with pytest.raises(ValueError, match="recording profile"):
        parse_profile("benchmark-special-case")


def test_the_allowlist_is_state_changing_calls_only():
    assert {"goto", "click", "fill_form", "scroll", "press_key"} <= ACTIONS
    assert not ({"snapshot", "see", "page_text", "js", "form_schema"} & ACTIONS)
    assert {"goto", "wait_lifecycle", "wait_for"} <= ALREADY_SETTLED


def test_loaded_navigation_frame_does_not_pay_an_extra_fixed_sleep(wired, monkeypatch):
    slept = []
    monkeypatch.setattr("evidence.record.time.sleep", slept.append)
    journal, _, _ = wired

    with journal.call("goto"):
        pass
    assert slept == []

    with journal.call("click"):
        pass
    assert slept == [0.15]


def test_the_capture_hook_does_not_serialise_parallel_workers(tmp_path):
    """The capture lock covers frame numbering only.

    It used to wrap the whole capture at the hook's call site, so ONE global Recorder
    serialised every parallel() worker across SETTLE (0.15 s) plus a screenshot round trip.
    Driving the real hook (`_on_call`) is the point — calling `_capture` directly would
    bypass exactly the lock that was the bug. Ten concurrent hook firings must overlap:
    serialised they cost >= 10 x SETTLE, overlapped roughly 1 x SETTLE.
    """
    import threading
    import time as _time

    from evidence import record as rec_mod
    from harness.core.journal import Journal, Span

    workers = 10

    class SlowTab:
        def capture_screenshot(self, path, **kw):
            _time.sleep(0.02)                    # stand in for the screenshot round trips
            path.write_bytes(b"\xff\xd8\xff")
            return path

        def js(self, *a, **kw):
            return {}

    tabs = [SlowTab() for _ in range(workers)]
    local = threading.local()
    recorder = rec_mod.Recorder(lambda: local.tab, Journal(tmp_path / "j.jsonl"), tmp_path)

    barrier = threading.Barrier(workers)
    errors: list[BaseException] = []

    def work(t):
        local.tab = t
        try:
            barrier.wait(timeout=5)
            recorder._on_call(Span(id="s", fn="click", started=_time.perf_counter()), {})
        except BaseException as e:               # noqa: BLE001 — surfaced by the assert below
            errors.append(e)

    threads = [threading.Thread(target=work, args=(t,)) for t in tabs]
    start = _time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    elapsed = _time.perf_counter() - start

    assert not errors, errors
    assert recorder.frames == workers, "every worker must have produced a frame"
    serialised = workers * rec_mod.SETTLE
    assert elapsed < serialised / 2, (
        f"{workers} hook captures took {elapsed:.2f}s; serialised would be >= {serialised:.2f}s")


def test_frame_numbers_stay_unique_and_contiguous_under_concurrency(tmp_path):
    """Shrinking the lock must not reintroduce a numbering race."""
    import threading
    import time as _time

    from evidence import record as rec_mod
    from harness.core.journal import Journal, Span

    names: list[str] = []
    lock = threading.Lock()

    class Tab:
        def capture_screenshot(self, path, **kw):
            with lock:
                names.append(path.name)
            path.write_bytes(b"\xff\xd8\xff")
            return path

        def js(self, *a, **kw):
            return {}

    tab = Tab()
    recorder = rec_mod.Recorder(lambda: tab, Journal(tmp_path / "j.jsonl"), tmp_path)
    local = threading.local()

    def work():
        local.tab = tab
        recorder._on_call(Span(id="s", fn="click", started=_time.perf_counter()), {})

    threads = [threading.Thread(target=work) for _ in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(names) == 24
    assert len(set(names)) == 24, "duplicate frame numbers — the counter raced"
    assert sorted(names) == [f"{i:04d}.jpg" for i in range(1, 25)]
    assert recorder.frames == 24
