"""`bh --doctor`: classify why the browser cannot be reached (TODO 14, D11).

v1's flagship misdiagnosis: any handshake stall became "click Allow in Chrome" — including
against a browser with **zero windows**, where no consent popup can exist. Rule 1 of the
outcome contract: never invent a cause you did not verify. So the doctor reports a class it
observed, plus per-class guidance, and where it cannot distinguish, it says so instead of
guessing.

The checks are websocket-free for the same reason discovery is: every ws connection to an
M144+ Chrome costs the user a consent prompt, and the daemon's one held connection is the
only one that should ever trigger it.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from urllib import request as urlrequest

from harness.connect.endpoint import binding_for, resolve, safe_endpoint
from harness.core.outcome import (
    RECOVERY,
    Class,
    HarnessError,
    Outcome,
    fail,
    ok,
)

#: What a human should do next, keyed by the class — never parsed, freely reworded.
#: The doctor's four endpoint classes live in the shared recovery map with every other
#: class, so a caller reading an outcome and a user reading `bh --doctor` are told the same
#: thing. Kept as a name here because it is the doctor's vocabulary.
GUIDANCE = RECOVERY


def count_pages(http_url: str, timeout: float = 3.0) -> int | None:
    """Page-target count via `/json/list`, or None where M147 hides it. None means
    *unknown*, and unknown is reported as unknown — not as either specific cause."""
    try:
        with urlrequest.urlopen(f"{http_url}/json/list", timeout=timeout) as r:
            targets = json.loads(r.read())
        return sum(1 for t in targets if t.get("type") == "page")
    except (OSError, ValueError):
        return None


def diagnose(name: str = "default", env: Mapping[str, str] | None = None) -> Outcome:
    """One outcome: the endpoint that would be used, or the typed reason there is none."""
    try:
        binding = binding_for(name, env)
        r = resolve(binding, env)
    except HarnessError as e:
        return e.outcome
    observed: dict[str, Any] = {"ws_url": r.ws_url, "strategy": r.strategy,
                                "attempts": [a.to_json() for a in r.attempts]}
    if r.http_url:
        pages = count_pages(r.http_url)
        observed["pages"] = pages
        if pages == 0:
            return fail(Class.NO_BROWSER_WINDOW,
                        f"{r.http_url} is live but reports zero page targets", **observed)
    return ok(None, **observed)


#: Any absolute ws/http URL, wherever it appears — including inside prose. `chrome://`
#: and bare paths are deliberately not matched: they name no credential and they are the
#: diagnosis.
_ENDPOINT_IN_TEXT = re.compile(r"(?:wss?|https?)://[^\s\"'<>,)]+")


def _scrub(value: Any) -> Any:
    """Reduce every endpoint in a payload to topology, at any depth.

    Redacting named fields one at a time loses this race: the pinned-endpoint failure puts
    the URL in `detail` as prose AND in `observed.pinned`, an HTTP probe puts it somewhere
    else again, and the next field added puts it somewhere nobody updated. What must hold
    is a property of the whole document — it is offered as safe to attach to an issue — so
    it is enforced over the whole document.
    """
    if isinstance(value, str):
        return _ENDPOINT_IN_TEXT.sub(lambda m: safe_endpoint(m.group(0)), value)
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def to_json(outcome: Outcome) -> dict[str, Any]:
    """The doctor's verdict for a machine, with no way in.

    `render()` keeps the full websocket URL because a terminal line is ephemeral and the
    URL is the diagnosis. This is the other case: JSON gets piped into files, attached to
    bug reports and pasted into issues, which is the same exposure the journal has — and
    the ws path is a capability, not an address.
    """
    return _scrub(outcome.to_json())


def render(outcome: Outcome) -> list[str]:
    """Human lines. Every attempt is shown — the losers' reasons are the diagnosis."""
    lines: list[str] = []
    if outcome.ok:
        lines.append(f"ok: {outcome.observed['ws_url']}  (via {outcome.observed['strategy']})")
        if outcome.observed.get("pages") is not None:
            lines.append(f"    {outcome.observed['pages']} page target(s)")
    else:
        lines.append(f"{outcome.cls.value}: {outcome.detail}")
    for a in outcome.observed.get("attempts", []):
        mark = "won" if a.get("won") else "declined"
        reason = f" — {a['reason']}" if a.get("reason") else ""
        lines.append(f"  {a['strategy']:>14}  {mark}: {a['candidate']}{reason}")
    if not outcome.ok and (advice := GUIDANCE.get(outcome.cls)):
        lines.append(f"next: {advice}")
    return lines
