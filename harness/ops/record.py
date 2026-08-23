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
from pathlib import Path
from typing import Any

from harness.core.journal import Journal, Span

#: Calls that change what is on screen. `see`/`capture_screenshot` are excluded because
#: they already produce an image on demand, and because recording one would recurse.
ACTIONS = frozenset({
    "goto", "click", "fill_form", "set_value", "press_key", "scroll", "upload_file",
    "wait_lifecycle", "wait_for",
})

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
                 max_dim: int = 900, quality: int = 65):
        self.dir = directory
        self.journal = journal
        self._tab_for = tab_for
        self.max_dim, self.quality = max_dim, quality
        self.frames = 0
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
        return self.dir

    # -- the hook ----------------------------------------------------------

    def _on_call(self, span: Span, payload: dict[str, Any]) -> dict[str, Any] | None:
        if span.fn not in ACTIONS or getattr(self._local, "busy", False):
            return None
        # The capture opens spans of its own; without this guard the hook would fire on
        # its own screenshot and recurse until the stack gave out.
        self._local.busy = True
        try:
            tab = self._tab_for()
            if tab is None:
                return None
            # Bind the tab before waiting: Session's current-tab cursor is thread-local,
            # and looking it up from a different thread later would capture the wrong page.
            return self._capture(tab, settle=span.fn not in ALREADY_SETTLED)
        except Exception:      # noqa: BLE001 — a recording must never break the run
            return None
        finally:
            self._local.busy = False

    def _capture(self, tab: Any, *, settle: bool = True) -> dict[str, Any] | None:
        # The lock covers frame numbering and NOTHING else. It used to wrap this whole
        # method, which made one global recorder serialise every worker: with BH_RECORD=1
        # and parallel() running 10 tabs, each capture held the lock across SETTLE plus a
        # screenshot round trip. A measured 100-job run took ~256 captures — roughly 40 s
        # of pure lock-held sleep, on ten threads that had nothing to contend over. The
        # sleep is per-tab paint time and the screenshot is per-target; only the counter is
        # shared.
        if settle:
            time.sleep(SETTLE)
        with self._capture_lock:
            self.frames += 1
            name = f"{self.frames:04d}.jpg"
        shot = tab.capture_screenshot(
            self.dir / name, max_dim=self.max_dim, quality=self.quality,
            include_context=True)
        extra: dict[str, Any] = {"frame": name}
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
          title: str | None = None, viewport: list[int] | None = None) -> Recorder:
    """Begin a recording and point the journal at it.

    The journal is *moved* into the recording directory, so the frames and the calls that
    produced them are one artifact rather than two that have to be correlated by timestamp.
    """
    name = name or time.strftime("rec-%Y%m%d-%H%M%S")
    directory = recordings_root() / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "meta.json").write_text(json.dumps({
        "name": name, "title": title, "started": round(time.time(), 3),
        "viewport": viewport}), encoding="utf-8")
    journal.path = directory / "session.jsonl"
    return Recorder(tab_for, journal, directory)


def prune(keep: int = 20) -> list[Path]:
    """Drop the oldest recordings past `keep`. Default-on recording without this is a
    disk leak; v1 rolls over on idle but never deletes."""
    import shutil
    doomed = recordings()[keep:]
    for d in doomed:
        shutil.rmtree(d, ignore_errors=True)
    return doomed
