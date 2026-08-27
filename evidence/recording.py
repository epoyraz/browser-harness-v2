"""Action recordings and screencasts, owned above the harness.

These were `Session` methods, which made every browser script carry two recorder handles
it would never use. Evidence is worth having and is not a browser primitive: the harness
navigates, reads and writes, and something else decides that a run deserves frames.

The lifetime still works, through `session.at_close()` — a recording belongs to a session,
and the harness offers a teardown hook without knowing what is registered on it. Handles
live here, keyed by session identity, so `Session` carries no attribute for them.
"""
from __future__ import annotations

import os
from functools import partial
from typing import Any

from evidence import record, screencast

_recorders: dict[int, Any] = {}
_screencasts: dict[int, Any] = {}

SURFACE = ("start_recording", "stop_recording", "start_screencast", "stop_screencast")


def install(namespace: dict[str, Any]) -> list[str]:
    """Bind the recording surface into a `bh` script namespace, curried on its session.

    Also honours `BH_RECORD`, which `Session.__init__` used to read directly — the env var
    keeps working for anyone who sets it, without the harness knowing why.
    """
    session = namespace.get("session")
    if session is None:
        raise RuntimeError("install() needs a `bh` namespace holding `session`")
    for name in SURFACE:
        namespace[name] = partial(globals()[name], session)
    autostart(session)
    return list(SURFACE)


def autostart(session: Any) -> str | None:
    """Start recording when `BH_RECORD` asks for it, and not otherwise."""
    raw = os.environ.get("BH_RECORD", "").strip().lower()
    if raw in ("", "0", "false", "no", "off"):
        return None
    # A profile name is also a convenient one-variable opt-in. Legacy truthy values
    # (especially BH_RECORD=1) retain the review profile unless the dedicated profile
    # variable says otherwise.
    selected = raw if raw in {profile.value for profile in record.Profile} else None
    return start_recording(session, profile=selected)


def start_recording(session: Any, name: str | None = None, title: str | None = None, *,
                    profile: str | record.Profile | None = None) -> str:
    """Record action evidence under an explicit evidence/review/cinematic profile.

    Turning it on *moves* the journal into the recording directory: the frames and the
    calls that produced them become one artifact instead of two to correlate.
    """
    current = _recorders.get(id(session))
    if current is not None:
        if profile is not None and record.parse_profile(profile) is not current.profile:
            raise ValueError(
                f"recording already uses profile {current.profile.value!r}")
        return str(current.dir)
    selected = record.parse_profile(
        profile if profile is not None else os.environ.get("BH_RECORD_PROFILE"))
    started = record.start(
        lambda: session._current and session._tabs.get(session._current),
        session.journal, name=name, title=title, profile=selected)
    _recorders[id(session)] = started
    session.at_close(partial(stop_recording, session))
    # Batch evidence runs may deliberately create more than the interactive default of 20
    # recordings. Keep rollover bounded while allowing the caller to preserve the whole
    # batch explicitly.
    keep = max(1, int(os.environ.get("BH_RECORDING_KEEP", "20")))
    record.prune(keep=keep)
    return str(started.dir)


def stop_recording(session: Any) -> str | None:
    current = _recorders.pop(id(session), None)
    if current is None:
        return None
    return str(current.stop())


def start_screencast(session: Any, name: str | None = None, *, quality: int = 88,
                     max_width: int = 1440, max_height: int = 1000,
                     every_nth_frame: int = 1) -> str:
    """Continuously capture compositor updates for the current tab through CDP."""
    current = _screencasts.get(id(session))
    if current is not None:
        return str(current.dir)
    started = screencast.start(
        session.tab(), name=name, quality=quality, max_width=max_width,
        max_height=max_height, every_nth_frame=every_nth_frame)
    _screencasts[id(session)] = started
    session.at_close(partial(stop_screencast, session))
    return str(started.dir)


def stop_screencast(session: Any) -> str | None:
    current = _screencasts.pop(id(session), None)
    if current is None:
        return None
    return str(current.stop())
