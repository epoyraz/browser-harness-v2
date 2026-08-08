"""Launching the scratch-profile Chrome that every live suite needs.

Extracted because the way you start Chrome turned out to matter, and eight copies of the
wrong way cost three permission resets in one session.

**Never launch Chrome as a child of the test process.** macOS attributes a subprocess's
file access to the *responsible process* — the terminal app — so Chrome reaching for
`~/Downloads` on startup raises a TCC prompt against the terminal. That prompt appears
behind the Chrome window, and an unanswered prompt is recorded as a denial: the terminal
loses access to Desktop/Documents, and every subsequent run dies reading its own
virtualenv, several frames deep, with an error that says nothing about Chrome.

`open -na` hands the launch to launchd, which owns the responsibility instead. The
explicit download directory removes the trigger as well, so neither half depends on the
other being enough.

The cost is that there is no process handle to terminate — hence `kill()`, which finds the
browser by the scratch profile it was told to use. No other Chrome can be holding it.

That last sentence is load-bearing and was once false on Windows, where `kill()` ran
`taskkill /F /IM chrome.exe` and took down every Chrome on the machine: the operator's own
browser, and the one an attached extension was driving, which killed the calling CLI too.
Both branches now match on `--user-data-dir=<profile>` so the blast radius is the scratch
profile and nothing else.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from harness.core.resources import BrowserLease, kill_chrome_for_profile

CHROME = (os.environ.get("BH_CHROME")
          or ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
              if sys.platform == "darwin" else shutil.which("google-chrome")
              or shutil.which("chromium") or "google-chrome"))

#: Windows-only: the occlusion calculator suspends renderers in offscreen windows, which
#: makes a headed CI run look like a hang.
FLAGS = ["--disable-features=CalculateNativeWinOcclusion"] if os.name == "nt" else []
_LEASES: dict[str, BrowserLease] = {}


def reserve(profile: Path) -> BrowserLease:
    """Reserve a scratch-browser slot for a specialised launcher."""
    key = str(profile.expanduser().resolve())
    lease = BrowserLease.acquire(profile)
    _LEASES[key] = lease
    lease.start_watchdog()
    return lease


def launch(profile: Path, *, window: str = "1200,800", extra: list[str] | None = None,
           timeout: float = 25.0) -> None:
    """Start Chrome on a scratch profile and wait until it publishes DevToolsActivePort.

    A scratch profile is not just hygiene: Chrome M144 grants remote-debugging consent per
    websocket and refuses a second one to an already-authorised browser (D7, measured 0/6),
    so a suite that attached to the daily driver could not get a connection at all.
    """
    key = str(profile.expanduser().resolve())
    lease = reserve(profile)
    downloads = profile / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    args = [f"--user-data-dir={profile}", "--remote-debugging-port=0",
            "--no-first-run", "--no-default-browser-check",
            f"--download-directory={downloads}", f"--window-size={window}",
            *FLAGS, *(extra or []), "about:blank"]
    headless = os.environ.get("BH_HEADLESS", "").lower() in {"1", "true", "yes"}
    if headless:
        args[0:0] = ["--headless=new", "--disable-gpu"]
    try:
        if os.name == "nt":                  # no `open`; the TCC problem is macOS-only
            subprocess.Popen([CHROME, *args],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform == "darwin" and not headless:
            subprocess.run(["/usr/bin/open", "-na", CHROME, "--args", *args],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen([CHROME, *args],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        while not (profile / "DevToolsActivePort").exists():
            if time.monotonic() > deadline:
                raise RuntimeError("Chrome never wrote DevToolsActivePort")
            time.sleep(0.1)
        time.sleep(0.4)      # the port file lands a moment before the socket accepts
    except Exception:
        _LEASES.pop(key, None)
        lease.release()
        raise


def kill(profile: Path) -> None:
    """Stop the browser started by `launch()`, identified by its scratch profile."""
    key = str(profile.expanduser().resolve())
    lease = _LEASES.pop(key, None)
    try:
        if lease is None:
            raise RuntimeError(f"refusing to kill unowned Chrome profile {profile}")
        # One implementation, shared with the lease watchdog: this existed twice, and both
        # copies ignored the profile on Windows.
        if os.name != "nt":
            subprocess.run(["/usr/bin/pkill", "-f", "--", f"--user-data-dir={profile}"],
                           check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.8)
        kill_chrome_for_profile(str(profile))
        time.sleep(0.2)
    finally:
        if lease is not None:
            lease.release()
