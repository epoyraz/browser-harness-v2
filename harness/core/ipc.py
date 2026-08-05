"""Client/daemon IPC (DESIGN.md item 6, §6).

**One `platform.system()` branch, never a transport abstraction.** Thirteen v1 PRs proposed
abstractions at +178 to +587 lines; the one that landed was +136/-54, and a
`BU_DAEMON_TRANSPORT` strategy layer was abandoned after weeks. That history is the spec.

AF_UNIX + 0600 on POSIX; TCP loopback + a per-daemon token on Windows, because
python-build-standalone omits `socket.AF_UNIX` there and loopback has no chmod equivalent.

Two constraints carried over from v1 because they were paid for in real failures:

  - **macOS `sun_path` is 104 bytes.** A caller-supplied dir plus a long name once produced
    a 117-char path and a bind failure, so runtime files (socket/port/pid — must be short)
    are separated from tmp files (logs/screenshots — may be deep).
  - **Bind under `umask 0077`**, not `chmod` after. The gap between `bind()` and `chmod()`
    is a TOCTOU window in which any local user can connect and issue CDP commands.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
from pathlib import Path
from typing import Any

IS_WINDOWS = sys.platform == "win32"

#: Endpoint names must be filesystem- and path-traversal-safe.
_NAME = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")

#: AF_UNIX sun_path limit on macOS. A path at or beyond this fails to bind.
SUN_PATH_MAX = 104


class IPCError(OSError):
    """Transport-level failure. Distinct from a CDP or page error."""


def check_name(name: str) -> str:
    if not _NAME.match(name or ""):
        raise ValueError(f"invalid endpoint name {name!r}: must match {_NAME.pattern}")
    return name


def runtime_dir() -> Path:
    """Where socket/port/pid live. Deliberately short — see SUN_PATH_MAX."""
    if raw := os.environ.get("BH_RUNTIME_DIR"):
        return Path(raw).expanduser()
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "browser-harness"
    return Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / f"bh-{os.getuid()}"


def ensure_private(path: Path) -> Path:
    """Create with 0700. The socket's directory is the real access boundary."""
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if not existed and not IS_WINDOWS:
        os.chmod(path, 0o700)
    return path


def sock_path(name: str) -> Path:
    p = ensure_private(runtime_dir()) / f"{check_name(name)}.sock"
    if not IS_WINDOWS and len(str(p).encode()) >= SUN_PATH_MAX:
        raise IPCError(
            f"socket path is {len(str(p).encode())} bytes, over the {SUN_PATH_MAX}-byte "
            f"AF_UNIX limit: {p}. Set BH_RUNTIME_DIR to something shorter."
        )
    return p


def port_path(name: str) -> Path:
    """Windows only: holds {"port", "token"}."""
    return ensure_private(runtime_dir()) / f"{check_name(name)}.port"


def read_port(name: str) -> tuple[int | None, str | None]:
    try:
        d = json.loads(port_path(name).read_text(encoding="utf-8"))
        return int(d["port"]), str(d["token"])
    except (OSError, ValueError, KeyError, TypeError):
        return None, None


def write_port(name: str, port: int, token: str) -> None:
    """Atomic, so a concurrent reader never sees a half-written file."""
    p = port_path(name)
    tmp = p.with_suffix(".port.tmp")
    tmp.write_text(json.dumps({"port": port, "token": token}), encoding="utf-8")
    os.replace(tmp, p)


def connect(name: str, timeout: float = 5.0) -> tuple[socket.socket, str | None]:
    """(socket, token). Token is None on POSIX, where AF_UNIX + 0600 is the boundary."""
    if not IS_WINDOWS:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(str(sock_path(name)))
        except OSError as e:
            s.close()
            raise IPCError(f"no daemon at {sock_path(name)}: {e}") from e
        return s, None
    port, token = read_port(name)
    if port is None:
        raise IPCError(f"no daemon: {port_path(name)} missing or unreadable")
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except OSError as e:
        raise IPCError(f"no daemon on 127.0.0.1:{port}: {e}") from e
    s.settimeout(timeout)
    return s, token


def request(sock: socket.socket, token: str | None, payload: dict[str, Any]) -> dict[str, Any]:
    """One newline-delimited JSON round trip. The caller owns the socket."""
    if token:
        payload = {**payload, "token": token}
    sock.sendall((json.dumps(payload, default=str) + "\n").encode())
    buf = bytearray()
    while not buf.endswith(b"\n"):
        chunk = sock.recv(1 << 16)
        if not chunk:
            break
        buf += chunk
    if not buf:
        raise IPCError("daemon closed the connection without replying")
    try:
        reply = json.loads(buf)
    except json.JSONDecodeError as e:
        raise IPCError(f"malformed reply: {bytes(buf[:120])!r}") from e
    if not isinstance(reply, dict):
        raise IPCError(f"reply was {type(reply).__name__}, expected object")
    return reply


def ping(name: str, timeout: float = 1.0) -> dict[str, Any] | None:
    """Liveness handshake, not a bare connect.

    A bare TCP connect succeeds against *any* process that grabbed the port after a crash,
    and a meta-only probe answers from a cached dict on a daemon whose CDP socket is dead —
    v1 needed six PRs to converge on "alive" meaning both. Only our daemon replies `pong`,
    and the reply states whether its browser connection is live.
    """
    try:
        sock, token = connect(name, timeout=timeout)
    except (IPCError, OSError):
        return None
    try:
        reply = request(sock, token, {"meta": "ping"})
        return reply if reply.get("pong") is True else None
    except (IPCError, OSError):
        return None
    finally:
        try:
            sock.close()
        except OSError:
            pass


def cleanup(name: str) -> None:
    """Best effort; silent if already gone. Never raises — including when the path
    itself is unrepresentable (an over-long sun_path must not block teardown)."""
    try:
        path = port_path(name) if IS_WINDOWS else sock_path(name)
    except (IPCError, OSError, ValueError):
        return
    try:
        path.unlink()
    except (FileNotFoundError, OSError):
        pass


def bind(name: str) -> socket.socket:
    """Listening socket, private from the first byte.

    POSIX: `umask 0077` around `bind()` so the socket is never briefly world-writable —
    a `chmod` afterwards leaves a window an attacker can win.
    """
    if IS_WINDOWS:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(64)
        return s
    path = sock_path(name)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o077)
    try:
        s.bind(str(path))
    finally:
        os.umask(old)
    s.listen(64)
    return s
