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


@dataclass(slots=True)
class Resolution:
    ws_url: str
    http_url: str          # "" when the strategy never had an HTTP side (M147 profiles)
    strategy: str
    attempts: list[Attempt] = field(default_factory=list)


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


def profile_dirs(env: Mapping[str, str]) -> list[Path]:
    """Candidate profile dirs. One env override replaces v1's 30 hardcoded vendor paths."""
    if raw := env.get("BH_PROFILE_DIRS"):
        return [Path(p).expanduser() for p in raw.split(os.pathsep) if p]
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
        return [base / "Google" / "Chrome", base / "Chromium"]
    if sys.platform == "win32":
        base = Path(env.get("LOCALAPPDATA", home))
        return [base / "Google" / "Chrome" / "User Data", base / "Chromium" / "User Data"]
    return [home / ".config" / "google-chrome", home / ".config" / "chromium"]


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
            return Resolution(ws, "", "explicit-ws", attempts)
        attempts.append(Attempt("explicit-ws", ws,
                                reason=f"nothing listening on {u.hostname}:{u.port}"))
    else:
        attempts.append(Attempt("explicit-ws", "BU_CDP_WS", reason="not set"))

    if http := env.get("BU_CDP_URL", ""):
        try:
            got = probe_http(http)
            attempts.append(Attempt("explicit-http", http, won=True))
            return Resolution(got["ws"], got["http"], "explicit-http", attempts)
        except HarnessError as e:
            attempts.append(Attempt("explicit-http", http, reason=e.args[0]))
    else:
        attempts.append(Attempt("explicit-http", "BU_CDP_URL", reason="not set"))

    for d in profile_dirs(env):
        cand = str(d / "DevToolsActivePort")
        found = read_active_port(d)
        if found is None:
            attempts.append(Attempt("profile", cand, reason="no DevToolsActivePort file"))
            continue
        port, path = found
        if not tcp_alive("127.0.0.1", port):
            attempts.append(Attempt("profile", cand, reason=(
                f"stale: nothing listening on 127.0.0.1:{port} — Chrome exited without "
                f"cleaning up")))
            continue
        attempts.append(Attempt("profile", cand, won=True))
        return Resolution(f"ws://127.0.0.1:{port}{path}", f"http://127.0.0.1:{port}",
                          "profile", attempts)

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
                              [Attempt("pinned", binding.url, won=True)])
        raise EndpointUnreachable(
            f"pinned endpoint {binding.url} is unreachable; staying pinned rather than "
            f"discovering another browser", pinned=binding.url)
    got = probe_http(binding.url)               # 404/refused raise typed, naming the pin
    return Resolution(got["ws"], got["http"], "pinned",
                      [Attempt("pinned", binding.url, won=True)])
