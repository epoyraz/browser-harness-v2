"""Explicit ownership and hard resource budgets.

Tabs and browser contexts are process-local resources; scratch Chrome instances are
machine resources. Both need the same property: only the owner may release them, and
cleanup must leave evidence instead of swallowing failures.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from harness.core import ipc
from harness.core.outcome import ResourceLimit

MAX_BROWSER_INSTANCES = 5


@dataclass(slots=True)
class _Owned:
    kind: str
    identifier: str
    release: Callable[[], None]


class ResourceLedger:
    """Thread-safe resource ownership with observable, reverse-order cleanup."""

    def __init__(self, *, journal: Any = None):
        self._journal = journal
        self._lock = threading.Lock()
        self._owned: dict[tuple[str, str], _Owned] = {}
        self._order: list[tuple[str, str]] = []

    def acquire(self, kind: str, identifier: str, release: Callable[[], None]) -> None:
        key = (kind, identifier)
        with self._lock:
            if key in self._owned:
                return
            self._owned[key] = _Owned(kind, identifier, release)
            self._order.append(key)
        self._write("resource_acquired", resource_kind=kind, identifier=identifier)

    def release(self, kind: str, identifier: str) -> dict[str, Any] | None:
        key = (kind, identifier)
        with self._lock:
            owned = self._owned.pop(key, None)
        if owned is None:
            return None
        try:
            owned.release()
        except Exception as exc:  # noqa: BLE001 — cleanup reports rather than masks work
            failure = {"kind": kind, "identifier": identifier,
                       "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            self._write("resource_cleanup_failed", resource_kind=kind,
                        identifier=identifier, error=failure["error"])
            return failure
        self._write("resource_released", resource_kind=kind, identifier=identifier)
        return None

    def cleanup(self) -> list[dict[str, Any]]:
        with self._lock:
            order = list(reversed(self._order))
            self._order.clear()
        failures = []
        for kind, identifier in order:
            if failure := self.release(kind, identifier):
                failures.append(failure)
        return failures

    def active(self) -> list[dict[str, str]]:
        with self._lock:
            return [{"kind": item.kind, "identifier": item.identifier}
                    for item in self._owned.values()]

    def _write(self, event: str, **payload: Any) -> None:
        if self._journal is not None:
            self._journal.write("note", event=event, **payload)


def _state_path() -> Path:
    raw = os.environ.get("BH_BROWSER_LEASE_DIR")
    root = Path(raw).expanduser() if raw else Path.home() / ".browser-harness" / "runtime"
    return ipc.ensure_private(root) / "browser-instances.json"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def _locked_state(*, prune: bool = True) -> Iterator[tuple[Path, list[dict[str, Any]]]]:
    """Lock the registry itself; the JSON file can then be replaced atomically."""
    lock_path = _state_path().with_suffix(".lock")
    lock_path.touch(mode=0o600, exist_ok=True)
    with lock_path.open("r+") as lock:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        path = _state_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            entries = raw if isinstance(raw, list) else []
        except (OSError, ValueError):
            entries = []
        if prune:
            entries = [entry for entry in entries if _pid_alive(int(entry.get("pid") or 0))]
        try:
            yield path, entries
        finally:
            path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class BrowserLease:
    """One owned scratch Chrome slot from the machine-wide budget of five."""

    def __init__(self, lease_id: str, profile: str):
        self.id = lease_id
        self.profile = profile
        self._released = False

    @classmethod
    def acquire(cls, profile: str | os.PathLike[str]) -> BrowserLease:
        resolved = str(Path(profile).expanduser().resolve())
        requested = int(os.environ.get("BH_BROWSER_LIMIT") or MAX_BROWSER_INSTANCES)
        limit = max(1, min(requested, MAX_BROWSER_INSTANCES))
        with _locked_state() as (_, entries):
            if any(entry.get("profile") == resolved for entry in entries):
                raise ResourceLimit("scratch Chrome profile is already owned",
                                    resource="browser", profile=resolved, limit=limit)
            if len(entries) >= limit:
                raise ResourceLimit(
                    f"refusing to launch Chrome {len(entries) + 1}: machine budget is {limit}",
                    resource="browser", active=len(entries), limit=limit,
                    profiles=[entry.get("profile") for entry in entries])
            lease_id = uuid.uuid4().hex
            entries.append({"id": lease_id, "pid": os.getpid(), "profile": resolved,
                            "started": round(time.time(), 3)})
        return cls(lease_id, resolved)

    def release(self) -> None:
        if self._released:
            return
        with _locked_state() as (_, entries):
            entries[:] = [entry for entry in entries if entry.get("id") != self.id]
        self._released = True

    def start_watchdog(self) -> None:
        """Kill this uniquely profiled Chrome if its launching process crashes."""
        if os.environ.get("BH_DISABLE_WATCHDOG", "").lower() in {"1", "true", "yes"}:
            return
        kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                                  "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP \
                | subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, "-m", "harness.core.resources", "watch", self.id],
                         **kwargs)

    @classmethod
    def active(cls) -> list[dict[str, Any]]:
        with _locked_state() as (_, entries):
            return [dict(entry) for entry in entries]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        self.release()
        return False


def _claim_dead_lease(lease_id: str) -> str | None:
    """Remove one dead owner's lease and return the Chrome profile it owned."""
    with _locked_state(prune=False) as (_, entries):
        entry = next((item for item in entries if item.get("id") == lease_id), None)
        if entry is None or _pid_alive(int(entry.get("pid") or 0)):
            return None
        entries.remove(entry)
        return str(entry.get("profile") or "")


def kill_chrome_for_profile(profile: str) -> None:
    """Stop only the Chrome started on `profile`. The scope is the whole point.

    Both callers used to reach for `taskkill /F /IM chrome.exe` on Windows, which is every
    Chrome on the machine — the operator's own tabs, and any browser an extension is
    driving. Measured on a developer box: 29 chrome.exe running, 0 of them the scratch
    profile. Killing an attached browser also took down the CLI that started the run, so
    the watchdog turned a crash into a bigger one.

    taskkill has no command-line filter, so match `--user-data-dir` explicitly and stop by
    pid — the same scoping the POSIX branch always had.
    """
    if not profile:
        return
    if os.name == "nt":
        quoted = str(profile).replace("'", "''")
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
            f"Where-Object {{ $_.CommandLine -like '*--user-data-dir={quoted}*' }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
            "-ErrorAction SilentlyContinue }"
        )
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.run(["/usr/bin/pkill", "-9", "-f", "--", f"--user-data-dir={profile}"],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _watch(lease_id: str) -> int:
    while True:
        with _locked_state(prune=False) as (_, entries):
            entry = next((item for item in entries if item.get("id") == lease_id), None)
        if entry is None:
            return 0
        if _pid_alive(int(entry.get("pid") or 0)):
            time.sleep(0.5)
            continue
        profile = _claim_dead_lease(lease_id)
        if not profile:
            return 0
        kill_chrome_for_profile(profile)
        return 0


if __name__ == "__main__":
    raise SystemExit(_watch(sys.argv[2]) if len(sys.argv) == 3 and sys.argv[1] == "watch" else 2)
