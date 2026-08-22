"""`bh mac-approve` — the macOS Allow-sheet approver (ported from v1 PR #610).

The behavioural contract worth defending, in order of how badly breaking it hurts:
  * it never runs osascript before the one-time Chrome checkbox is on,
  * it never `activate`s Chrome (approving must not steal the user's focus),
  * it only ever presses a button inside a sheet titled exactly "Allow remote debugging?",
  * a missing Accessibility grant is reported as such, not as a generic failure,
  * and finding no sheet is NOT reported as a pending permission (outcome rule 1).
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.connect import macos
from harness.core.outcome import Class


@pytest.fixture
def on_darwin(monkeypatch):
    monkeypatch.setattr(macos.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(macos, "_already_reachable", lambda env: False)


def _toggle_on(monkeypatch):
    monkeypatch.setattr(macos, "toggle_enabled_profiles", lambda env=None: [Path("/tmp/Chrome")])


def _osascript(monkeypatch, *, stdout="", stderr="", returncode=0, record=None):
    def run(*args, **kwargs):
        if record is not None:
            record.append((args, kwargs))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    monkeypatch.setattr(macos.subprocess, "run", run)


def test_off_macos_it_declines_instead_of_pretending(monkeypatch):
    monkeypatch.setattr(macos.platform, "system", lambda: "Linux")
    out = macos.approve_remote_debugging()
    assert not out.ok and out.cls is Class.PLATFORM_UNSUPPORTED
    assert out.observed["status"] == "unsupported"


def test_a_reachable_browser_needs_no_approval_and_no_osascript(monkeypatch):
    monkeypatch.setattr(macos.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(macos, "_already_reachable", lambda env: True)
    called = []
    _osascript(monkeypatch, stdout="ready", record=called)
    out = macos.approve_remote_debugging()
    assert out.ok and out.observed == {"status": "ready", "clicked": False}
    assert called == [], "osascript must not run when the browser already answers"


def test_the_one_time_checkbox_is_demanded_before_any_ui_poking(on_darwin, monkeypatch):
    monkeypatch.setattr(macos, "toggle_enabled_profiles", lambda env=None: [])
    called = []
    _osascript(monkeypatch, stdout="ready", record=called)

    out = macos.approve_remote_debugging()

    assert not out.ok and out.cls is Class.ENDPOINT_UNREACHABLE
    assert out.observed["status"] == "setup-required"
    assert "chrome://inspect/#remote-debugging" in out.detail
    assert called == [], "the checkbox is not reachable over CDP — never guess past it"


def test_it_presses_only_the_exact_sheet_and_never_activates_chrome(on_darwin, monkeypatch):
    _toggle_on(monkeypatch)
    called = []
    _osascript(monkeypatch, stdout="ready\n", record=called)

    out = macos.approve_remote_debugging()

    assert out.ok and out.observed == {"status": "ready", "clicked": True}
    (args, kwargs) = called[0]
    assert args == (["osascript"],)
    script = kwargs["input"]
    assert 'is "Allow remote debugging?"' in script
    assert "AXPress" in script
    assert "activate" not in script, "approving must never steal focus"
    assert kwargs["timeout"] == 5


def test_no_sheet_is_not_reported_as_a_pending_permission(on_darwin, monkeypatch):
    """Outcome rule 1: never invent a cause you did not verify."""
    _toggle_on(monkeypatch)
    _osascript(monkeypatch, stdout="not-found\n")

    out = macos.approve_remote_debugging()

    assert not out.ok
    assert out.cls is Class.APPROVAL_NOT_PENDING
    assert out.cls is not Class.PERMISSION_PENDING
    assert out.observed["status"] == "not-found"
    assert "retry the browser command" in out.detail


def test_a_sheet_answered_by_hand_mid_flight_still_reports_ready(on_darwin, monkeypatch):
    _toggle_on(monkeypatch)
    _osascript(monkeypatch, stdout="not-found\n")
    seen = iter([False, True])   # not reachable at entry, reachable by the re-check
    monkeypatch.setattr(macos, "_already_reachable", lambda env: next(seen))

    out = macos.approve_remote_debugging()

    assert out.ok and out.observed["clicked"] is False


def test_a_hung_osascript_reads_as_a_missing_accessibility_grant(on_darwin, monkeypatch):
    _toggle_on(monkeypatch)

    def hang(*args, **kwargs):
        raise macos.subprocess.TimeoutExpired(cmd="osascript", timeout=5)
    monkeypatch.setattr(macos.subprocess, "run", hang)

    out = macos.approve_remote_debugging()

    assert out.cls is Class.HOST_PERMISSION_REQUIRED
    assert out.observed["status"] == "accessibility-required"
    assert "Accessibility" in out.detail


@pytest.mark.parametrize("stderr", [
    "osascript is not authorized to send Apple events",
    "assistive access is not enabled",
])
def test_osascript_authorisation_errors_name_the_real_fix(on_darwin, monkeypatch, stderr):
    _toggle_on(monkeypatch)
    _osascript(monkeypatch, returncode=1, stderr=stderr)

    out = macos.approve_remote_debugging()

    assert out.cls is Class.HOST_PERMISSION_REQUIRED
    assert "System Settings" in out.detail


def test_an_unrecognised_osascript_result_is_not_silently_a_success(on_darwin, monkeypatch):
    _toggle_on(monkeypatch)
    _osascript(monkeypatch, stdout="banana\n")

    out = macos.approve_remote_debugging()

    assert not out.ok and out.cls is Class.CDP_ERROR
    assert "banana" in out.detail


def test_toggle_profiles_reads_local_state_and_survives_junk(tmp_path, monkeypatch):
    good, bad, absent = tmp_path / "good", tmp_path / "bad", tmp_path / "absent"
    good.mkdir(); bad.mkdir()
    (good / "Local State").write_text(
        '{"devtools": {"remote_debugging": {"user-enabled": true}}}')
    (bad / "Local State").write_text("{not json")
    monkeypatch.setattr(macos, "profile_dirs", lambda env: [good, bad, absent])

    assert macos.toggle_enabled_profiles({}) == [good]


def test_a_profile_with_the_toggle_off_does_not_count(tmp_path, monkeypatch):
    p = tmp_path / "off"; p.mkdir()
    (p / "Local State").write_text(
        '{"devtools": {"remote_debugging": {"user-enabled": false}}}')
    monkeypatch.setattr(macos, "profile_dirs", lambda env: [p])

    assert macos.toggle_enabled_profiles({}) == []


# -- automatic approval ------------------------------------------------------

def test_auto_approval_is_macos_only_and_opt_outable(monkeypatch):
    monkeypatch.setattr(macos.platform, "system", lambda: "Darwin")
    assert macos.auto_approve_enabled({}) is True
    assert macos.auto_approve_enabled({"BH_MAC_APPROVE": "0"}) is False
    monkeypatch.setattr(macos.platform, "system", lambda: "Linux")
    assert macos.auto_approve_enabled({}) is False


def test_arm_is_a_no_op_where_it_does_not_apply(monkeypatch):
    monkeypatch.setattr(macos.platform, "system", lambda: "Linux")
    assert macos.arm(macos.threading.Event()) is None


def test_arm_stops_pressing_once_the_sheet_is_answered(monkeypatch):
    monkeypatch.setattr(macos.platform, "system", lambda: "Darwin")
    calls = []

    def approve(env=None, *, assume_pending=False):
        calls.append(1)
        from harness.core.outcome import ok
        return ok(None, status="ready")
    monkeypatch.setattr(macos, "approve_remote_debugging", approve)

    stop = macos.threading.Event()
    t = macos.arm(stop, attempts=5, interval=0.01, env={})
    t.join(timeout=2)

    assert not t.is_alive()
    assert len(calls) == 1, "a successful press must not keep polling"


def test_arm_gives_up_on_a_missing_accessibility_grant_instead_of_spinning(monkeypatch):
    monkeypatch.setattr(macos.platform, "system", lambda: "Darwin")
    calls = []

    def approve(env=None, *, assume_pending=False):
        calls.append(1)
        from harness.core.outcome import fail
        return fail(Class.HOST_PERMISSION_REQUIRED, "grant it")
    monkeypatch.setattr(macos, "approve_remote_debugging", approve)

    stop = macos.threading.Event()
    t = macos.arm(stop, attempts=8, interval=0.01, env={})
    t.join(timeout=2)

    assert not t.is_alive() and len(calls) == 1


def test_arm_never_lets_a_helper_crash_reach_the_daemon(monkeypatch):
    monkeypatch.setattr(macos.platform, "system", lambda: "Darwin")

    def boom(env=None, *, assume_pending=False):
        raise RuntimeError("osascript exploded")
    monkeypatch.setattr(macos, "approve_remote_debugging", boom)

    stop = macos.threading.Event()
    t = macos.arm(stop, attempts=3, interval=0.01, env={})
    t.join(timeout=2)

    assert not t.is_alive()


def test_stopping_the_event_ends_the_pump(monkeypatch):
    monkeypatch.setattr(macos.platform, "system", lambda: "Darwin")
    calls = []

    def approve(env=None, *, assume_pending=False):
        calls.append(1)
        from harness.core.outcome import fail
        return fail(Class.APPROVAL_NOT_PENDING, "no sheet")
    monkeypatch.setattr(macos, "approve_remote_debugging", approve)

    stop = macos.threading.Event()
    t = macos.arm(stop, attempts=100, interval=0.01, env={})
    stop.set()
    t.join(timeout=2)

    assert not t.is_alive()


def test_render_matches_v1s_one_line_shape():
    from harness.core.outcome import fail, ok
    assert macos.render(ok(None, status="ready")) == ["ready"]
    assert macos.render(fail(Class.APPROVAL_NOT_PENDING, "retry it", status="not-found")) == [
        "not-found: retry it"]


def test_an_armed_approver_presses_the_sheet_even_though_the_endpoint_answers(monkeypatch):
    """The bug this test exists for.

    The Allow sheet guards the WEBSOCKET; Chrome serves `/json/version` over HTTP without
    any consent. So during a live handshake `_already_reachable` is True *while a sheet is
    on screen waiting to be pressed*. The first cut short-circuited on it and reported
    `ready` without ever running the AppleScript — the auto-approval was a no-op in exactly
    the case it existed for.
    """
    monkeypatch.setattr(macos.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(macos, "_already_reachable", lambda env: True)   # endpoint answers
    _toggle_on(monkeypatch)
    called = []
    _osascript(monkeypatch, stdout="ready\n", record=called)

    out = macos.approve_remote_debugging(assume_pending=True)

    assert called, "armed approval must run the AppleScript even when the endpoint answers"
    assert out.ok and out.observed["clicked"] is True


def test_no_sheet_yet_keeps_the_armed_pump_looking(monkeypatch):
    """A reachable endpoint must not end the pump on its first tick: the handshake has to
    reach Chrome before Chrome can ask."""
    monkeypatch.setattr(macos.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(macos, "_already_reachable", lambda env: True)
    _toggle_on(monkeypatch)
    _osascript(monkeypatch, stdout="not-found\n")

    out = macos.approve_remote_debugging(assume_pending=True)

    assert out.cls is Class.APPROVAL_NOT_PENDING, "armed mode must not report ready here"


def test_the_cli_still_short_circuits_when_the_browser_already_answers(monkeypatch):
    """Unarmed, the question really is 'is there anything for me to do?' — and there isn't."""
    monkeypatch.setattr(macos.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(macos, "_already_reachable", lambda env: True)
    called = []
    _osascript(monkeypatch, stdout="ready\n", record=called)

    out = macos.approve_remote_debugging()

    assert out.ok and out.observed["clicked"] is False
    assert called == []


def test_arm_keeps_polling_until_a_sheet_appears(monkeypatch):
    """Three ticks with no sheet, then one with — the pump must survive the first two."""
    monkeypatch.setattr(macos.platform, "system", lambda: "Darwin")
    from harness.core.outcome import fail as _fail
    from harness.core.outcome import ok as _ok
    seen = []

    def approve(env=None, *, assume_pending=False):
        seen.append(assume_pending)
        if len(seen) < 4:
            return _fail(Class.APPROVAL_NOT_PENDING, "no sheet", status="not-found")
        return _ok(None, status="ready", clicked=True)
    monkeypatch.setattr(macos, "approve_remote_debugging", approve)

    stop = macos.threading.Event()
    t = macos.arm(stop, attempts=10, interval=0.01, env={})
    t.join(timeout=3)

    assert not t.is_alive()
    assert len(seen) == 4, f"pump stopped early after {len(seen)} ticks"
    assert all(seen), "arm() must always ask in assume_pending mode"


def test_arm_gives_up_when_the_chrome_checkbox_is_off(monkeypatch):
    """setup-required is not fixable by looking again — that checkbox needs a human."""
    monkeypatch.setattr(macos.platform, "system", lambda: "Darwin")
    from harness.core.outcome import fail as _fail
    calls = []

    def approve(env=None, *, assume_pending=False):
        calls.append(1)
        return _fail(Class.ENDPOINT_UNREACHABLE, "tick the box", status="setup-required")
    monkeypatch.setattr(macos, "approve_remote_debugging", approve)

    stop = macos.threading.Event()
    t = macos.arm(stop, attempts=10, interval=0.01, env={})
    t.join(timeout=3)

    assert not t.is_alive() and len(calls) == 1
