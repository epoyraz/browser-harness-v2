"""Endpoint discovery and binding (DESIGN.md D8/D10, TODO 11–12).

Discovery is a **flat ranked list**, not a nested fallback ladder: explicit ws URL, then
explicit HTTP URL, then liveness-probed `DevToolsActivePort` files from profile dirs. Every
strategy leaves an `Attempt` saying whether it won or why it declined — v1's ladder could
tell you nothing when it failed, which is why its diagnostics grew to guesswork.

Probes never open a websocket. Chrome M144 shows a consent prompt **per ws connection**,
and the daemon's single held connection is the only one we ever open (D7). HTTP and bare
TCP prompt for nothing, so probing is free; a ws-handshake "liveness check" would cost the
user a popup per probe.

Chrome M147 disables `/json/*` on the default profile. The profile strategy therefore does
not use HTTP at all: `DevToolsActivePort` holds the port and the browser ws path directly.

Trust (D10): a **pinned** binding never widens scope. Pinned-and-dead is an error naming
the endpoint; it must never fall through to discovery, because discovery finding the user's
daily-driver Chrome is exactly how a harness ends up driving someone's real browser (#479).
The binding is persisted per daemon name, so a daemon respawned *without its env* stays
pinned instead of silently becoming a discoverer.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit

from harness.core import ipc
from harness.core.outcome import (
    Endpoint404,
    EndpointUnreachable,
    HarnessError,
    ScopeRefused,
)

PINNED = "pinned"
DISCOVER = "discover"


@dataclass(slots=True)
class Attempt:
    """One strategy's verdict. The losers' reasons are the diagnostic."""

    strategy: str
    candidate: str
    won: bool = False
    reason: str = ""

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"strategy": self.strategy, "candidate": self.candidate,
                             "won": self.won}
        if self.reason:
            d["reason"] = self.reason
        return d


def safe_endpoint(url: str) -> str:
    """An endpoint's topology, with nothing that grants access to it.

    A CDP websocket URL is a bearer capability, not an address: the browser GUID in
    `ws://127.0.0.1:9222/devtools/browser/<guid>` drives the browser for anyone who can
    reach the port, and a `BU_CDP_WS` pointed at a remote endpoint can carry a provider
    token in the path or query outright. Scheme, host and port are what a reader needs to
    diagnose "which browser did it find"; the rest is the credential.

    Ported from browser-harness v1 (`_safe_connection_label`, v0.1.10), where the same URL
    was going into a plaintext daemon log.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "<redacted-endpoint>"
    if not parsed.scheme or not parsed.hostname:
        return "<redacted-endpoint>"
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    try:
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        return "<redacted-endpoint>"          # a port that will not parse is not topology
    return f"{parsed.scheme}://{host}{port}"


@dataclass(slots=True)
class Resolution:
    ws_url: str
    http_url: str          # "" when the strategy never had an HTTP side (M147 profiles)
    strategy: str
    attempts: list[Attempt] = field(default_factory=list)
    identity: BrowserIdentity | None = None


@dataclass(frozen=True, slots=True)
class BrowserIdentity:
    """Mechanically resolved owner of a local browser endpoint.

    The macOS consent sheet is native UI, outside CDP's target model.  A websocket URL
    alone therefore cannot scope an AXPress: the listener PID is the capability that ties
    the sheet to the endpoint we resolved.  ``application`` is checked as a second factor
    so a recycled PID cannot silently widen the action to an unrelated process.
    """

    pid: int | None
    application: str
    profile_dir: str = ""
    ws_url: str = ""


_MAC_PROFILE_APPLICATIONS = (
    ("Library/Application Support/Google/Chrome Canary", "Google Chrome Canary"),
    ("Library/Application Support/Google/Chrome", "Google Chrome"),
    ("Library/Application Support/Comet", "Comet"),
    ("Library/Application Support/Arc/User Data", "Arc"),
    ("Library/Application Support/Dia/User Data", "Dia"),
    ("Library/Application Support/Microsoft Edge Beta", "Microsoft Edge Beta"),
    ("Library/Application Support/Microsoft Edge Dev", "Microsoft Edge Dev"),
    ("Library/Application Support/Microsoft Edge Canary", "Microsoft Edge Canary"),
    ("Library/Application Support/Microsoft Edge", "Microsoft Edge"),
    ("Library/Application Support/BraveSoftware/Brave-Browser", "Brave Browser"),
    ("Library/Application Support/Chromium", "Chromium"),
)


def _mac_application_for_profile(profile_dir: Path | None) -> str:
    if profile_dir is None:
        return ""
    # POSIX form on purpose: the table's suffixes are written with `/`, and a test that
    # pins `sys.platform` to darwin on a Windows host hands this a WindowsPath whose `str`
    # uses backslashes — measured 2026-08-28, that alone reported every known profile as
    # application "". On a real Mac `as_posix()` is the identity.
    path = profile_dir.expanduser().as_posix()
    for suffix, application in _MAC_PROFILE_APPLICATIONS:
        if path.endswith(suffix):
            return application
    return ""


def mac_listener_pid(ws_url: str) -> int | None:
    """Return the one local process listening for this endpoint, or fail closed.

    ``lsof`` is part of macOS and exposes the kernel-owned port-to-PID relation without
    opening another websocket.  Zero or multiple owners are deliberately not guessed.
    """
    if sys.platform != "darwin":
        return None
    parsed = urlsplit(ws_url)
    host = (parsed.hostname or "").lower()
    if host not in {"127.0.0.1", "localhost", "::1"} or parsed.port is None:
        return None
    try:
        completed = subprocess.run(
            ["/usr/sbin/lsof", "-nP", "-a", f"-iTCP@{host}:{parsed.port}",
             "-sTCP:LISTEN", "-Fp"],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    pids = {
        int(line[1:])
        for line in completed.stdout.splitlines()
        if line.startswith("p") and line[1:].isdigit()
    }
    return next(iter(pids)) if len(pids) == 1 else None


def _mac_process_name(pid: int | None) -> str:
    if sys.platform != "darwin" or pid is None:
        return ""
    try:
        completed = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "comm="],
            capture_output=True, text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    command = completed.stdout.strip()
    return Path(command).name if command else ""


def _endpoint_profile_matches(ws_url: str, env: Mapping[str, str] | None) -> list[Path]:
    """Profiles whose active-port record names this exact browser websocket."""
    parsed = urlsplit(ws_url)
    if parsed.port is None:
        return []
    expected = (parsed.port, parsed.path)
    env = os.environ if env is None else env
    return [base for base in profile_dirs(env) if read_active_port(base) == expected]


def browser_identity(ws_url: str, profile_dir: Path | None = None,
                     env: Mapping[str, str] | None = None) -> BrowserIdentity | None:
    """Resolve the local macOS UI process that owns ``ws_url`` without opening it."""
    if sys.platform != "darwin":
        return None
    pid = mac_listener_pid(ws_url)
    if profile_dir is None:
        matches = _endpoint_profile_matches(ws_url, env)
        profile_dir = matches[0] if len(matches) == 1 else None
    application = _mac_application_for_profile(profile_dir) or _mac_process_name(pid)
    return BrowserIdentity(
        pid=pid,
        application=application,
        profile_dir=str(profile_dir.expanduser()) if profile_dir is not None else "",
        ws_url=ws_url,
    )


# -- probes (websocket-free, see module docstring) --------------------------

def tcp_alive(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def probe_http(url: str, timeout: float = 3.0) -> dict[str, str]:
    """`/json/version` round trip. Raises the typed error for what actually happened."""
    base = url.rstrip("/")
    try:
        with urlrequest.urlopen(f"{base}/json/version", timeout=timeout) as r:
            body = r.read()
    except urlerror.HTTPError as e:
        if e.code == 404:
            raise Endpoint404(
                f"{base}/json/version is 404 — Chrome M147 disables /json/* on the "
                f"default profile", url=base, status=404) from e
        raise EndpointUnreachable(f"{base}/json/version answered HTTP {e.code}",
                                  url=base, status=e.code) from e
    except (urlerror.URLError, TimeoutError, OSError) as e:
        raise EndpointUnreachable(f"nothing answering at {base}: {getattr(e, 'reason', e)}",
                                  url=base) from e
    try:
        info = json.loads(body)
        ws = info["webSocketDebuggerUrl"]
    except (ValueError, KeyError, TypeError) as e:
        # A 200 that is not DevTools is the stale-port trap: after a crash, any process
        # can be squatting on the recorded port. Rule 1 — do not call it a browser.
        raise EndpointUnreachable(
            f"{base} answered, but not as a DevTools endpoint (another process on the "
            f"port?)", url=base) from e
    return {"ws": ws, "http": base, "browser": str(info.get("Browser", ""))}


def read_active_port(profile_dir: Path) -> tuple[int, str] | None:
    """Parse `DevToolsActivePort`: line 1 the port, line 2 the browser ws path."""
    try:
        lines = (profile_dir / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()
        port = int(lines[0])
        path = lines[1]
    except (OSError, ValueError, IndexError):
        return None
    if not path.startswith("/"):
        return None
    return port, path


# -- profile tables ----------------------------------------------------------
#
# v1 carried these ten paths per platform (v1 src/browser_harness/daemon.py:38-77). v2's
# first cut replaced them with Chrome + Chromium and called BH_PROFILE_DIRS the answer.
# That is only an answer for someone who already knows the variable exists: a Brave / Edge
# / Arc / Comet / Canary / Flatpak user got a bare "endpoint unreachable" with no hint that
# their browser had never been looked for at all. The tables are back.
#
# The list order *is* the probe order, and mainstream Chrome is deliberately first on every
# platform: the overwhelmingly common case must win on candidate #1 and never pay for the
# long tail behind it. The tail is nearly free — a candidate whose directory does not exist
# costs one stat() and never reaches the port file (see discover()).

_MAC_PROFILES = (
    "Library/Application Support/Google/Chrome",
    "Library/Application Support/Google/Chrome Canary",
    "Library/Application Support/Comet",
    "Library/Application Support/Arc/User Data",
    "Library/Application Support/Dia/User Data",
    "Library/Application Support/Microsoft Edge",
    "Library/Application Support/Microsoft Edge Beta",
    "Library/Application Support/Microsoft Edge Dev",
    "Library/Application Support/Microsoft Edge Canary",
    "Library/Application Support/BraveSoftware/Brave-Browser",
    # Not in v1's mac table, but v2 has probed it since its first cut. Restoring v1's list
    # must not quietly drop a path v2 users already rely on, so it stays — at the back.
    "Library/Application Support/Chromium",
)
_LINUX_PROFILES = (
    ".config/google-chrome",
    ".config/chromium",
    ".config/chromium-browser",          # Debian/Ubuntu's package name for the same browser
    ".config/microsoft-edge",
    ".config/microsoft-edge-beta",
    ".config/microsoft-edge-dev",
    # Flatpak relocates the whole config tree per app id; without these four a Flatpak-only
    # desktop looks browserless no matter how many windows are open.
    ".var/app/org.chromium.Chromium/config/chromium",
    ".var/app/com.google.Chrome/config/google-chrome",
    ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser",
    ".var/app/com.microsoft.Edge/config/microsoft-edge",
)
_WINDOWS_PROFILES = (                    # relative to %LOCALAPPDATA%; SxS = Canary channel
    "Google/Chrome/User Data",
    "Google/Chrome SxS/User Data",
    "Google/Chrome Beta/User Data",
    "Google/Chrome Dev/User Data",
    "Chromium/User Data",
    "Microsoft/Edge/User Data",
    "Microsoft/Edge Beta/User Data",
    "Microsoft/Edge Dev/User Data",
    "Microsoft/Edge SxS/User Data",
    "BraveSoftware/Brave-Browser/User Data",
)


def profile_dirs(env: Mapping[str, str]) -> list[Path]:
    """Candidate profile dirs in probe order, Chrome first. `BH_PROFILE_DIRS` **replaces**
    the whole table (os.pathsep-separated) — that is the escape hatch for a throwaway
    `--user-data-dir`, not the thing a normal user is expected to discover."""
    if raw := env.get("BH_PROFILE_DIRS"):
        return [Path(p).expanduser() for p in raw.split(os.pathsep) if p]
    home = Path.home()
    if sys.platform == "darwin":
        return [home / p for p in _MAC_PROFILES]
    if sys.platform == "win32":
        # Every Chromium fork on Windows puts User Data under %LOCALAPPDATA%; the ~ fallback
        # is what a stripped service env leaves us holding.
        base = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local").expanduser()
        return [base / p for p in _WINDOWS_PROFILES]
    return [home / p for p in _LINUX_PROFILES]


def _tilde(p: Path) -> str:
    """Home-relative form for diagnosis lines. Ten absolute mac paths on one line is a
    wall; ten `~/Library/...` paths is a list a human reads."""
    try:
        return f"~/{p.relative_to(Path.home())}"
    except (ValueError, OSError, RuntimeError):
        return str(p)


# -- discovery (TODO 11) -----------------------------------------------------

def discover(env: Mapping[str, str] | None = None) -> Resolution:
    """Walk the ranked list. Returns the winner plus every verdict, or raises
    `EndpointUnreachable` carrying every verdict — the losers are the diagnosis."""
    env = os.environ if env is None else env
    attempts: list[Attempt] = []

    if ws := env.get("BU_CDP_WS", ""):
        u = urlsplit(ws)
        if u.port and tcp_alive(u.hostname or "127.0.0.1", u.port):
            attempts.append(Attempt("explicit-ws", ws, won=True))
            return Resolution(ws, "", "explicit-ws", attempts,
                              identity=browser_identity(ws, env=env))
        attempts.append(Attempt("explicit-ws", ws,
                                reason=f"nothing listening on {u.hostname}:{u.port}"))
    else:
        attempts.append(Attempt("explicit-ws", "BU_CDP_WS", reason="not set"))

    if http := env.get("BU_CDP_URL", ""):
        try:
            got = probe_http(http)
            attempts.append(Attempt("explicit-http", http, won=True))
            return Resolution(got["ws"], got["http"], "explicit-http", attempts,
                              identity=browser_identity(got["ws"], env=env))
        except HarnessError as e:
            attempts.append(Attempt("explicit-http", http, reason=e.args[0]))
    else:
        attempts.append(Attempt("explicit-http", "BU_CDP_URL", reason="not set"))

    # doctor.render prints every attempt verbatim, one line each. With a two-entry table
    # that was fine; with eleven it would bury the one line that matters under ten
    # identical "no DevToolsActivePort file" lines. A directory that does not exist means
    # that browser is not installed, which is not a diagnosis anyone needs per-vendor — so
    # those roll up into a single attempt, emitted below only when nothing won. Nothing is
    # hidden: the rollup names every path it stands for.
    absent: list[Path] = []
    for d in profile_dirs(env):
        if not d.is_dir():
            absent.append(d)
            continue
        cand = str(d / "DevToolsActivePort")
        found = read_active_port(d)
        if found is None:
            # This one *is* per-vendor news: the browser is installed and was not started
            # with remote debugging. Saying only "no DevToolsActivePort file" made users
            # think the file was missing rather than the flag.
            attempts.append(Attempt("profile", cand, reason=(
                "profile exists but is not running with remote debugging (no "
                "DevToolsActivePort file)")))
            continue
        port, path = found
        if not tcp_alive("127.0.0.1", port):
            attempts.append(Attempt("profile", cand, reason=(
                f"stale: nothing listening on 127.0.0.1:{port} — Chrome exited without "
                f"cleaning up")))
            continue
        attempts.append(Attempt("profile", cand, won=True))
        ws_url = f"ws://127.0.0.1:{port}{path}"
        return Resolution(ws_url, f"http://127.0.0.1:{port}", "profile", attempts,
                          identity=browser_identity(ws_url, d, env))

    if absent:
        attempts.append(Attempt("profile", f"{len(absent)} profile dir(s) not present",
                                reason="no such browser installed: "
                                       + ", ".join(_tilde(p) for p in absent)))
    raise EndpointUnreachable("no strategy produced a live endpoint",
                              attempts=[a.to_json() for a in attempts])


# -- binding (TODO 12) -------------------------------------------------------

@dataclass(slots=True)
class Binding:
    mode: str
    url: str = ""

    @staticmethod
    def from_env(env: Mapping[str, str]) -> Binding:
        """An explicit endpoint in the env *is* a pin. Naming a browser and then attaching
        to a different one is never opportunism, it is a scope violation."""
        if url := env.get("BU_CDP_WS", ""):
            return Binding(PINNED, url)
        if url := env.get("BU_CDP_URL", ""):
            return Binding(PINNED, url)
        if env.get("BH_TRUST", "") == PINNED:
            return Binding(PINNED, "")          # pinned with nothing to pin to: see resolve()
        return Binding(DISCOVER)

    def save(self, name: str) -> None:
        path = ipc.ensure_private(ipc.runtime_dir()) / f"{ipc.check_name(name)}.binding"
        path.write_text(json.dumps({"mode": self.mode, "url": self.url}), encoding="utf-8")

    @staticmethod
    def load(name: str) -> Binding | None:
        path = ipc.runtime_dir() / f"{ipc.check_name(name)}.binding"
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            return Binding(str(d["mode"]), str(d.get("url", "")))
        except (OSError, ValueError, KeyError, TypeError):
            return None


def binding_for(name: str, env: Mapping[str, str] | None = None) -> Binding:
    """The #479 rule: pins persist. An env pin is recorded; a respawn that lost its env
    finds the recorded pin and stays pinned instead of silently becoming a discoverer.
    `BH_TRUST=discover` is the one deliberate way back out."""
    env = os.environ if env is None else env
    fresh = Binding.from_env(env)
    if fresh.mode == PINNED:
        fresh.save(name)
        return fresh
    if env.get("BH_TRUST", "") == DISCOVER:
        fresh.save(name)                        # explicit intent overwrites the old pin
        return fresh
    saved = Binding.load(name)
    if saved is not None and saved.mode == PINNED:
        return saved
    return fresh


def resolve(binding: Binding, env: Mapping[str, str] | None = None) -> Resolution:
    """Pinned probes its one endpoint and nothing else; discover walks the list."""
    if binding.mode != PINNED:
        return discover(env)
    if not binding.url:
        raise ScopeRefused(
            "binding is pinned but names no endpoint; refusing to discover a browser "
            "this daemon was never granted (#479)")
    u = urlsplit(binding.url)
    if u.scheme in ("ws", "wss"):
        if u.port and tcp_alive(u.hostname or "127.0.0.1", u.port):
            return Resolution(binding.url, "", "pinned",
                              [Attempt("pinned", binding.url, won=True)],
                              identity=browser_identity(binding.url, env=env))
        raise EndpointUnreachable(
            f"pinned endpoint {binding.url} is unreachable; staying pinned rather than "
            f"discovering another browser", pinned=binding.url)
    got = probe_http(binding.url)               # 404/refused raise typed, naming the pin
    return Resolution(got["ws"], got["http"], "pinned",
                      [Attempt("pinned", binding.url, won=True)],
                      identity=browser_identity(got["ws"], env=env))
