"""Session recording: one frame per action, hung off the journal (v1 parity, D11b).

A recording is a folder:

    <recordings>/<name>/
      meta.json       {name, title, started, viewport}
      session.jsonl   the journal — with `frame` on the calls that produced one
      0001.jpg …      viewport screenshot after each state-changing action

**The journal *is* the events file.** v1 keeps a separate `events.jsonl` carrying ts,
helper, args, url and frame — every field of which its trace already knew — so the two have
to be joined back together to answer anything. v2's journal already has `ts`, `id`, `fn`,
`args`, `ms`, `cdp`, `outcome` and `parent`; recording adds `frame` to that same entry via
`Journal.on_call`. One file, and `bh trace` renders it unchanged.

Only state-changing calls get a frame. Read-only ones (`snapshot`, `page_text`,
`form_schema`, `js`) would bloat an inspection-heavy session with identical images and add
no visual beat — v1 learned this and keeps the same allowlist.

Failures never propagate: a recording that breaks a run is worse than no recording.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import Counter
from enum import StrEnum
from pathlib import Path
from typing import Any

from harness.core.journal import Journal, Span

#: Calls that change what is on screen. `see`/`capture_screenshot` are excluded because
#: they already produce an image on demand, and because recording one would recurse.
#: ``review`` is the historical recorder contract.  Keep this name as an alias because
#: callers and tests imported it before profiles existed.
ACTIONS = frozenset({
    "goto", "click", "fill_form", "set_value", "press_key", "scroll", "upload_file",
    "wait_lifecycle", "wait_for",
})

#: High-level helpers whose completion can have a visible consequence.  Some wrap calls in
#: ``ACTIONS``: an ARIA select, for example, opens and chooses through two nested clicks.
#: Keeping this semantic and site-independent is what lets evidence mode collapse those
#: intermediate beats without learning benchmark ids, answer strings, or selectors.
CONSEQUENCE_ACTIONS = ACTIONS | frozenset({
    "click_auth_ref", "follow_application", "select_option", "set_secret", "type_chars",
    "wait_for_application_state", "wait_for_form",
})


class Profile(StrEnum):
    """How much visual state an action recording retains."""

    EVIDENCE = "evidence"
    REVIEW = "review"
    CINEMATIC = "cinematic"


def parse_profile(value: str | Profile | None) -> Profile:
    """Resolve one public profile spelling, rejecting typos instead of guessing."""
    if isinstance(value, Profile):
        return value
    raw = str(value or Profile.REVIEW.value).strip().lower()
    try:
        return Profile(raw)
    except ValueError as error:
        choices = ", ".join(profile.value for profile in Profile)
        raise ValueError(f"recording profile must be one of {choices}, got {value!r}") from error

# These helpers return only after the state they were waiting for is observable. Sleeping a
# further fixed 150 ms before every navigation frame cost 25.7 seconds in one 171-page
# research task without making the already-loaded pages more valid.
ALREADY_SETTLED = frozenset({"goto", "wait_lifecycle", "wait_for"})

#: Credential-bearing query/fragment params. Auth redirects otherwise land real secrets in
#: a folder people share — carried over from v1 verbatim, because it was earned there.
_URL_SECRETS = re.compile(
    r"([?&#](?:code|access_token|id_token|refresh_token|token|assertion"
    r"|client_secret|client_info|session_state|api_?key|sig|signature"
    r"|auth|authorization|password|secret)=)[^&#]+",
    re.IGNORECASE,
)

#: Let the page paint before the post-action frame. The one place a fixed sleep is right:
#: there is no event for "the compositor has finished a repaint I care about".
SETTLE = 0.15


def scrub(url: str) -> str:
    return _URL_SECRETS.sub(r"\1REDACTED", str(url))


def recordings_root() -> Path:
    if raw := os.environ.get("BH_RECORDINGS"):
        return Path(raw).expanduser()
    return Path.home() / ".browser-harness" / "recordings"


def recordings() -> list[Path]:
    """Recording directories, newest first."""
    root = recordings_root()
    if not root.is_dir():
        return []
    found = [p for p in root.iterdir() if p.is_dir() and (p / "meta.json").exists()]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def latest() -> Path | None:
    found = recordings()
    return found[0] if found else None


class Recorder:
    """Captures a frame as each state-changing call closes.

    Installed on the journal, not on `Tab`, so no primitive knows recording exists — which
    is also why a helper an agent writes themselves is recorded for free.
    """

    def __init__(self, tab_for: Any, journal: Journal, directory: Path, *,
                 profile: str | Profile = Profile.REVIEW,
                 max_dim: int = 900, quality: int = 65):
        self.dir = directory
        self.journal = journal
        self._tab_for = tab_for
        self.profile = parse_profile(profile)
        self.max_dim, self.quality = max_dim, quality
        self.frames = 0
        self.screenshot_ms = 0.0
        self.recording_ms = 0.0
        self.cdp_calls = 0
        self.bytes = 0
        self.suppressed: Counter[str] = Counter()
        self._allocated = 0
        self._local = threading.local()
        self._capture_lock = threading.Lock()
        # Bind ONCE. `self._on_call` builds a fresh bound-method object on every access,
        # so the identity check in stop() could never match and the recorder went on
        # capturing after it was stopped.
        self._hook = self._on_call
        journal.on_call = self._hook

    def stop(self) -> Path:
        if self.journal.on_call is self._hook:
            self.journal.on_call = None
        with self._capture_lock:
            summary = {
                "event": "recording_summary", "recording_profile": self.profile.value,
                "frames": self.frames, "frame_screenshot_ms": round(self.screenshot_ms, 1),
                "frame_recording_ms": round(self.recording_ms, 1),
                "frame_cdp": self.cdp_calls, "frame_bytes": self.bytes,
                "frame_suppressed": dict(sorted(self.suppressed.items())),
            }
        self.journal.write("note", **summary)
        return self.dir

    # -- the hook ----------------------------------------------------------

    def _on_call(self, span: Span, payload: dict[str, Any]) -> dict[str, Any] | None:
        if getattr(self._local, "busy", False) or span.fn not in CONSEQUENCE_ACTIONS:
            return None
        if self.profile is Profile.REVIEW and span.fn not in ACTIONS:
            return self._suppression(span, "profile_policy")
        # Evidence retains one final consequence frame for a high-level helper and records
        # why its nested visual mechanics were omitted.  The parent is still on this
        # thread's journal stack while the child's on_call hook runs.
        parent = self.journal.current
        if (self.profile is Profile.EVIDENCE and parent is not None
                and parent.fn in CONSEQUENCE_ACTIONS):
            return self._suppression(span, "nested_consequence")
        # The capture opens spans of its own; without this guard the hook would fire on
        # its own screenshot and recurse until the stack gave out.
        self._local.busy = True
        try:
            tab = self._tab_for()
            if tab is None:
                return self._suppression(span, "no_target")
            # Bind the tab before waiting: Session's current-tab cursor is thread-local,
            # and looking it up from a different thread later would capture the wrong page.
            return self._capture(tab, span, settle=span.fn not in ALREADY_SETTLED)
        except Exception as error:  # noqa: BLE001 — a recording must never break the run
            return self._suppression(span, "capture_failed",
                                     error_class=type(error).__name__)
        finally:
            self._local.busy = False

    def _suppression(self, span: Span, reason: str, **extra: Any) -> dict[str, Any]:
        with self._capture_lock:
            self.suppressed[reason] += 1
        return {
            "recording_profile": self.profile.value,
            "frame_span_id": span.id,
            "frame_suppressed": reason,
            **extra,
        }

    def _capture(self, tab: Any, span: Span, *, settle: bool = True) \
            -> dict[str, Any] | None:
        # The lock covers frame numbering and NOTHING else. It used to wrap this whole
        # method, which made one global recorder serialise every worker: with BH_RECORD=1
        # and parallel() running 10 tabs, each capture held the lock across SETTLE plus a
        # screenshot round trip. A measured 100-job run took ~256 captures — roughly 40 s
        # of pure lock-held sleep, on ten threads that had nothing to contend over. The
        # sleep is per-tab paint time and the screenshot is per-target; only the counter is
        # shared.
        recording_started = time.perf_counter()
        if settle:
            time.sleep(SETTLE)
        with self._capture_lock:
            self._allocated += 1
            name = f"{self._allocated:04d}.jpg"
        target_id = str(getattr(tab, "target_id", "") or "")
        screenshot_started = time.perf_counter()
        # The child screenshot span and its CDP trace rows are explicitly marked as
        # observability.  Stats/bench can then remove them from browser-work totals while
        # the triggering helper remains a normal span.
        with self.journal.bind(observability="recording", recording_span_id=span.id,
                               target_id=target_id or None):
            shot = tab.capture_screenshot(
                self.dir / name, max_dim=self.max_dim, quality=self.quality,
                include_context=True)
        screenshot_ms = (time.perf_counter() - screenshot_started) * 1000
        recording_ms = (time.perf_counter() - recording_started) * 1000
        shot = shot if isinstance(shot, dict) else {}
        frame_bytes = int(shot.get("bytes") or 0)
        frame_cdp = int(shot.get("cdp_calls") or 0)
        with self._capture_lock:
            self.frames += 1
            self.screenshot_ms += screenshot_ms
            self.recording_ms += recording_ms
            self.cdp_calls += frame_cdp
            self.bytes += frame_bytes
        extra: dict[str, Any] = {
            "frame": name,
            "frame_span_id": span.id,
            "frame_target_id": target_id or None,
            "recording_profile": self.profile.value,
            "frame_screenshot_ms": round(screenshot_ms, 1),
            "frame_recording_ms": round(recording_ms, 1),
            "frame_cdp": frame_cdp,
            "frame_bytes": frame_bytes,
        }
        try:
            # Screenshot capture already needs viewport/DPR from the page. Its same
            # evaluation carries the focused-element box, URL, and title, avoiding a
            # second Runtime.evaluate for every recorded action.
            ctx = shot.get("context") if isinstance(shot, dict) else {}
            ctx = ctx or {}
            if ctx.get("u"):
                extra["url"] = scrub(ctx["u"])
            if ctx.get("t"):
                extra["title"] = str(ctx["t"])[:120]
            if ctx.get("box"):
                extra["box"] = [round(v) for v in ctx["box"]]
        except Exception:  # noqa: BLE001, S110 — the frame is the point; context is a bonus
            pass
        return extra


def start(tab_for: Any, journal: Journal, *, name: str | None = None,
          title: str | None = None, viewport: list[int] | None = None,
          profile: str | Profile = Profile.REVIEW) -> Recorder:
    """Begin a recording and point the journal at it.

    The journal is *moved* into the recording directory, so the frames and the calls that
    produced them are one artifact rather than two that have to be correlated by timestamp.
    """
    name = name or time.strftime("rec-%Y%m%d-%H%M%S")
    directory = recordings_root() / name
    directory.mkdir(parents=True, exist_ok=True)
    selected = parse_profile(profile)
    (directory / "meta.json").write_text(json.dumps({
        "name": name, "title": title, "started": round(time.time(), 3),
        "viewport": viewport, "recording_profile": selected.value}), encoding="utf-8")
    journal.path = directory / "session.jsonl"
    return Recorder(tab_for, journal, directory, profile=selected)


def prune(keep: int = 20) -> list[Path]:
    """Drop the oldest recordings past `keep`. Default-on recording without this is a
    disk leak; v1 rolls over on idle but never deletes."""
    import shutil
    doomed = recordings()[keep:]
    for d in doomed:
        shutil.rmtree(d, ignore_errors=True)
    return doomed
