"""Hard browser budgets and cleanup evidence."""
from __future__ import annotations

import json
import os

import pytest

from harness.core.outcome import ResourceLimit
from harness.core.resources import BrowserLease, ResourceLedger, _claim_dead_lease


@pytest.fixture(autouse=True)
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_BROWSER_LEASE_DIR", str(tmp_path))


def test_machine_budget_refuses_a_sixth_browser(tmp_path):
    leases = [BrowserLease.acquire(tmp_path / f"p{i}") for i in range(5)]
    try:
        with pytest.raises(ResourceLimit) as error:
            BrowserLease.acquire(tmp_path / "p5")
        assert error.value.observed["limit"] == 5
        assert error.value.observed["active"] == 5
    finally:
        for lease in leases:
            lease.release()


def test_config_can_lower_but_never_raise_the_hard_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_BROWSER_LIMIT", "1")
    lease = BrowserLease.acquire(tmp_path / "one")
    try:
        with pytest.raises(ResourceLimit):
            BrowserLease.acquire(tmp_path / "two")
    finally:
        lease.release()


def test_stale_process_leases_are_pruned(tmp_path, monkeypatch):
    path = tmp_path / "browser-instances.json"
    path.write_text('[{"id":"dead","pid":99999999,"profile":"/dead"}]')
    lease = BrowserLease.acquire(tmp_path / "live")
    try:
        assert [entry["pid"] for entry in BrowserLease.active()] == [os.getpid()]
    finally:
        lease.release()


def test_watchdog_claims_a_crashed_owners_profile(tmp_path):
    path = tmp_path / "browser-instances.json"
    path.write_text('[{"id":"dead","pid":99999999,"profile":"/owned-profile"}]')
    assert _claim_dead_lease("dead") == "/owned-profile"
    assert json.loads(path.read_text()) == []


def test_ledger_cleans_up_in_reverse_order_and_reports_failures():
    seen = []
    ledger = ResourceLedger()
    ledger.acquire("tab", "a", lambda: seen.append("a"))
    ledger.acquire("tab", "b", lambda: (_ for _ in ()).throw(RuntimeError("stuck")))
    failures = ledger.cleanup()
    assert seen == ["a"]
    assert failures == [{"kind": "tab", "identifier": "b",
                         "error": "RuntimeError: stuck"}]
    assert ledger.active() == []


# --- the liveness probe must never signal anything ---------------------------

def test_pid_alive_detects_a_live_and_a_dead_process():
    """`os.kill(pid, 0)` reads as a probe and is one on POSIX. On Windows Python maps the
    signal onto the console API, signal.CTRL_C_EVENT == 0, and the event goes to every
    process sharing the console — so the probe raised Ctrl+C on the terminal running the
    suite and killed the whole CLI session. The watchdog polls this twice a second."""
    import os
    import subprocess
    import sys

    from harness.core.resources import _pid_alive

    assert _pid_alive(os.getpid()) is True
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    assert _pid_alive(done.pid) is False
    assert _pid_alive(0) is False
    assert _pid_alive(-1) is False


def test_pid_alive_does_not_signal_on_windows():
    """Pin the implementation, not just the answer: on Windows this must not reach os.kill
    at all, because every value of sig there either terminates the target or raises a
    console control event."""
    import inspect
    import os

    from harness.core import resources

    src = inspect.getsource(resources._pid_alive)
    windows_branch = src.split('if os.name == "nt":', 1)
    assert len(windows_branch) == 2, "the Windows branch is gone; os.kill would be reached"
    before_posix_fallback = windows_branch[1].split("try:", 1)[0]
    assert "os.kill" not in before_posix_fallback
    assert "OpenProcess" in before_posix_fallback
    if os.name == "nt":
        assert _win_probe_is_used(resources)


def _win_probe_is_used(resources):
    return resources._pid_alive(4) is True      # System: exists, access denied
