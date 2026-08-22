"""macOS-only: answer Chrome's per-connection "Allow remote debugging?" sheet.

Ported from v1's `browser_harness.macos` (PR #610), with three deliberate changes:

  1. **It returns an `Outcome`, not a `(status, detail)` tuple.** v1's caller had to
     string-match `"setup-required"` to branch, which is the `str` disease D11 exists to
     kill. The status string survives as `observed["status"]` for humans and for CLI
     parity, but recovery branches on `Outcome.cls`.
  2. **Profile discovery is `endpoint.profile_dirs`, not a hardcoded root.** v1 accepted
     the toggle only from `~/Library/Application Support/Google/Chrome`; here the same
     `BH_PROFILE_DIRS` override that steers discovery also steers this, so a Chromium or
     custom-profile user is not silently told "setup-required" forever.
  3. **Readiness is `doctor.diagnose`, not `daemon_browser_ready`.** The question "can we
     already reach the browser?" already has one answer in v2, and it is the doctor's.

Rule 1 of the outcome contract binds here in particular: `PERMISSION_PENDING` is returned
only when the AppleScript *observed* the sheet. A run that finds no sheet reports
`APPROVAL_NOT_PENDING` — "there was nothing to approve", not "you must click something".
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path

from harness.connect.endpoint import BrowserIdentity, mac_listener_pid, profile_dirs
from harness.core.outcome import Class, HarnessError, Outcome, fail, ok

#: Walks only the resolved PID's accessibility tree and presses the button whose
#: description is "Allow".  It never calls `activate`; approving must not steal focus
#: from whatever the user is doing.
_APPLESCRIPT = r'''using terms from application "System Events"
    on clickAllow(nodeRef)
        try
            if (role of nodeRef as text) is "AXButton" and ¬
                (description of nodeRef as text) is "Allow" then
                perform action "AXPress" of nodeRef
                return true
            end if
        end try
        try
            repeat with childRef in UI elements of nodeRef
                if my clickAllow(childRef) then return true
            end repeat
        end try
        return false
    end clickAllow
end using terms from

on run argv
    set targetPid to (item 1 of argv) as integer
    set expectedApplication to item 2 of argv
    set resultText to "not-found"
    tell application "System Events"
        set matchingProcesses to every process whose unix id is targetPid
        if (count of matchingProcesses) is not 1 then return "process-not-found"
        set browserProcess to item 1 of matchingProcesses
        if (name of browserProcess as text) is not expectedApplication then ¬
            return "identity-mismatch"
        tell browserProcess
            repeat with w in windows
                try
                    repeat with s in sheets of w
                        if (name of s as text) is "Allow remote debugging?" then
                            if my clickAllow(s) then
                                set resultText to "ready"
                                exit repeat
                            end if
                        end if
                    end repeat
                end try
                if resultText is "ready" then exit repeat
            end repeat
        end tell
    end tell
    return resultText
end run
'''

_ACCESSIBILITY_DETAIL = (
    "grant Accessibility to the app launching bh (Terminal, iTerm, an IDE, or Codex) in "
    "System Settings > Privacy & Security > Accessibility, then run `bh mac-approve` again"
)

_SETUP_DETAIL = (
    'first enable "Allow remote debugging for this browser instance" at '
    "chrome://inspect/#remote-debugging, then run `bh mac-approve` again"
)

_RETRY_DETAIL = "retry the browser command and run `bh mac-approve` while the prompt is up"

_IDENTITY_DETAIL = (
    "the resolved endpoint could not be tied to one local browser PID, so no consent "
    "sheet was touched"
)


def toggle_enabled_profiles(env: Mapping[str, str] | None = None) -> list[Path]:
    """Profile dirs whose chrome://inspect toggle is recorded on in `Local State`.

    The checkbox is a one-time manual step by design — it is not reachable over CDP, which
    is the whole reason this helper cannot simply do everything for the user.
    """
    env = os.environ if env is None else env
    out: list[Path] = []
    for base in profile_dirs(env):
        try:
            state = json.loads(
                (base / "Local State").read_text(encoding="utf-8", errors="replace"))
            devtools = (state.get("devtools") or {}).get("remote_debugging") or {}
            if devtools.get("user-enabled") is True:
                out.append(base)
        except (OSError, ValueError, AttributeError):
            continue
    return out


def _already_reachable(env: Mapping[str, str] | None) -> bool:
    """True when the browser is already answering — nothing to approve."""
    from harness.connect.doctor import diagnose
    try:
        return diagnose("default", env).ok
    except Exception:            # noqa: BLE001 — a doctor failure must not mask the sheet
        return False


def approve_remote_debugging(env: Mapping[str, str] | None = None, *,
                             identity: BrowserIdentity | None = None,
                             assume_pending: bool = False) -> Outcome:
    """Press Chrome's Allow sheet without activating Chrome. One shot, no polling.

    `assume_pending` skips the "is the browser already answering?" short-circuit. That
    check asks whether the HTTP endpoint responds — and Chrome answers `/json/version`
    without any consent at all, because the sheet guards the **websocket**, not the
    endpoint. So during a live handshake the short-circuit is not merely useless, it is
    actively wrong: it reports `ready` at exactly the moment a sheet is on screen waiting
    to be pressed. `arm()` therefore always passes it; the CLI never does, because there
    the question really is "is there anything for me to do?".
    """
    if platform.system() != "Darwin":
        return fail(Class.PLATFORM_UNSUPPORTED,
                    "mac-approve is only available on macOS",
                    status="unsupported", platform=platform.system())

    if not assume_pending and _already_reachable(env):
        return ok(None, status="ready", clicked=False)

    if (identity is None or identity.pid is None or not identity.application
            or not identity.ws_url):
        return fail(Class.SCOPE_REFUSED, _IDENTITY_DETAIL,
                    status="identity-required", clicked=False)

    if mac_listener_pid(identity.ws_url) != identity.pid:
        return fail(
            Class.SCOPE_REFUSED,
            "the resolved endpoint is no longer owned by the same browser PID; no consent "
            "sheet was touched",
            status="endpoint-owner-changed", clicked=False, pid=identity.pid,
            application=identity.application,
        )

    if not identity.profile_dir:
        return fail(
            Class.SCOPE_REFUSED,
            "the endpoint owner was found, but its remote-debugging profile was not; "
            "no consent sheet was touched",
            status="profile-required", clicked=False, pid=identity.pid,
            application=identity.application,
        )

    enabled_profiles = toggle_enabled_profiles(env)
    target_profile = Path(identity.profile_dir).expanduser()
    if target_profile not in enabled_profiles:
        return fail(Class.ENDPOINT_UNREACHABLE, _SETUP_DETAIL, status="setup-required")

    try:
        completed = subprocess.run(
            ["osascript", "-", str(identity.pid), identity.application],
            input=_APPLESCRIPT, text=True,
            capture_output=True, timeout=5, check=False)
    except subprocess.TimeoutExpired:
        # osascript hanging for 5s on a one-shot tree walk is what a missing Accessibility
        # grant looks like from here: the prompt is modal to *us*, not to Chrome.
        return fail(Class.HOST_PERMISSION_REQUIRED, _ACCESSIBILITY_DETAIL,
                    status="accessibility-required")
    except (OSError, subprocess.SubprocessError) as exc:
        return fail(Class.CDP_ERROR, str(exc), status="error")

    if completed.returncode != 0:
        detail = completed.stderr.strip() or "osascript failed"
        low = detail.lower()
        if "not authorized" in low or "assistive" in low:
            return fail(Class.HOST_PERMISSION_REQUIRED, _ACCESSIBILITY_DETAIL,
                        status="accessibility-required", stderr=detail)
        return fail(Class.CDP_ERROR, detail, status="error")

    status = completed.stdout.strip()
    if status == "ready":
        return ok(None, status="ready", clicked=True, pid=identity.pid,
                  application=identity.application)
    if status == "identity-mismatch":
        return fail(
            Class.SCOPE_REFUSED,
            "the endpoint PID now belongs to a different application; no consent sheet "
            "was touched",
            status="identity-mismatch", clicked=False, pid=identity.pid,
            application=identity.application,
        )
    if status == "process-not-found":
        return fail(
            Class.APPROVAL_NOT_PENDING,
            "the resolved browser process exited before approval; no consent sheet was "
            "touched",
            status="process-not-found", clicked=False, pid=identity.pid,
            application=identity.application,
        )
    if status == "not-found":
        # The user may have answered the sheet by hand while AppleScript was looking. Under
        # `assume_pending` that consolation does not apply for the same reason as above —
        # a reachable endpoint says nothing about a websocket's sheet — and reporting
        # `ready` here would stop the pump on its very first tick, before Chrome has even
        # been asked. No sheet yet simply means: look again.
        if not assume_pending and _already_reachable(env):
            return ok(None, status="ready", clicked=False)
        return fail(Class.APPROVAL_NOT_PENDING, _RETRY_DETAIL, status="not-found")
    return fail(Class.CDP_ERROR, f"unexpected osascript result: {status or '<empty>'}",
                status="error")


def render(outcome: Outcome) -> list[str]:
    """v1-compatible one-liner: `status` alone, or `status: what to do next`."""
    status = outcome.observed.get("status", "ok" if outcome.ok else outcome.cls.value)
    return [f"{status}: {outcome.detail}" if outcome.detail else status]


def run_cli(args: list[str]) -> int:
    if args:
        print("usage: bh mac-approve", flush=True)
        return 2
    outcome = approve_remote_debugging()
    if (outcome.cls is Class.SCOPE_REFUSED
            and outcome.observed.get("status") == "identity-required"):
        try:
            # Resolve exactly as the default daemon would, but do not open a websocket.
            # The listener PID scopes the native UI action to that browser instance.
            from harness.connect.endpoint import binding_for, resolve
            identity = resolve(binding_for("default"), os.environ).identity
            outcome = approve_remote_debugging(identity=identity)
        except HarnessError as exc:
            outcome = exc.outcome
    for line in render(outcome):
        print(line, flush=True)
    return 0 if outcome.ok else 1


# -- automatic approval (what v1 never had) ----------------------------------
#
# v1 shipped the helper but no call site: `b8bf24a "Use automatic macOS approval by
# default"` changed SKILL.md only, so "automatic" meant *the agent is told to run it*.
# The daemon's handshake is the one place that knows a prompt is imminent, so that is
# where the approver belongs. Opt out with BH_MAC_APPROVE=0.

def auto_approve_enabled(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return platform.system() == "Darwin" and env.get("BH_MAC_APPROVE", "1") != "0"


def arm(stop: threading.Event, *, identity: BrowserIdentity | None = None,
        pending: threading.Event | None = None, attempts: int = 12,
        interval: float = 0.5,
        env: Mapping[str, str] | None = None) -> threading.Thread | None:
    """Press the sheet in the background while the caller's handshake blocks on it.

    The sheet does not exist until the websocket handshake starts, and the handshake does
    not return until the sheet is answered — so the approver cannot run before or after,
    only *alongside*. Returns None when auto-approval does not apply.

    Never raises: this is a convenience, and a failure here must leave the handshake's own
    error as the reported cause rather than masking it.
    """
    if (not auto_approve_enabled(env) or identity is None or identity.pid is None
            or not identity.application or not identity.profile_dir or not identity.ws_url
            or pending is None):
        return None

    def _pump() -> None:
        for _ in range(attempts):
            if stop.wait(interval):
                return
            # The daemon owns this event and holds it only around construction of the one
            # new websocket transport.  A reusable approver must never outlive that exact
            # handshake and start inspecting native UI for some later browser operation.
            if not pending.is_set():
                continue
            try:
                # assume_pending: we were armed *because* a handshake is in flight, so the
                # endpoint being reachable proves nothing about the sheet. See above.
                outcome = approve_remote_debugging(
                    env, identity=identity, assume_pending=True)
            except Exception:        # noqa: BLE001 — a helper must not break the daemon
                return
            # Stop on success, and on the three states retrying cannot fix. Notably NOT on
            # APPROVAL_NOT_PENDING: no sheet yet is the normal first tick — the handshake
            # has to reach Chrome before Chrome can ask.
            if outcome.ok or outcome.cls in (
                    Class.PLATFORM_UNSUPPORTED, Class.HOST_PERMISSION_REQUIRED,
                    Class.ENDPOINT_UNREACHABLE, Class.SCOPE_REFUSED):
                return

    t = threading.Thread(target=_pump, name="bh-mac-approve", daemon=True)
    t.start()
    return t
