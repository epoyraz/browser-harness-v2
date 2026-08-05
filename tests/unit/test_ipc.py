"""IPC tests. Each maps to a v1 failure that cost at least one PR."""
import json
import os
import stat
import threading

import pytest

from harness.core import ipc


@pytest.fixture(autouse=True)
def short_runtime_dir(monkeypatch):
    """pytest's tmp_path on macOS is ~128 bytes — over the AF_UNIX limit this module
    enforces. The fixture has to obey the constraint it is testing."""
    import shutil
    import tempfile
    d = tempfile.mkdtemp(prefix="bh", dir="/tmp" if not ipc.IS_WINDOWS else None)
    monkeypatch.setenv("BH_RUNTIME_DIR", d)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _serve_once(sock, reply, *, expect=None):
    """Accept one connection, optionally capture the request, send `reply`."""
    def run():
        conn, _ = sock.accept()
        with conn:
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            if expect is not None:
                expect.append(json.loads(buf))
            conn.sendall((json.dumps(reply) + "\n").encode())
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


# --- name validation is a path-traversal guard ------------------------------

@pytest.mark.parametrize("bad", ["", "../etc", "a/b", "x" * 65, "a b", "n\x00ul"])
def test_bad_names_are_rejected(bad):
    with pytest.raises(ValueError):
        ipc.check_name(bad)


def test_good_names_pass():
    for good in ("default", "agent-1", "a_b", "X" * 64):
        assert ipc.check_name(good) == good


# --- the macOS 104-byte sun_path limit (v1 #244, #318) ----------------------

@pytest.mark.skipif(ipc.IS_WINDOWS, reason="AF_UNIX only")
def test_overlong_socket_path_fails_loudly_not_at_bind(tmp_path, monkeypatch):
    deep = tmp_path / ("d" * 60) / ("e" * 60)   # tmp_path is already long on macOS
    monkeypatch.setenv("BH_RUNTIME_DIR", str(deep))
    with pytest.raises(ipc.IPCError) as e:
        ipc.sock_path("default")
    assert "104" in str(e.value) and "BH_RUNTIME_DIR" in str(e.value)


@pytest.mark.skipif(ipc.IS_WINDOWS, reason="AF_UNIX only")
def test_normal_path_is_within_budget():
    assert len(str(ipc.sock_path("default")).encode()) < ipc.SUN_PATH_MAX


# --- the socket is private from the first byte (v1 #298/#309 TOCTOU) --------

@pytest.mark.skipif(ipc.IS_WINDOWS, reason="POSIX permissions")
def test_runtime_dir_is_0700():
    d = ipc.ensure_private(ipc.runtime_dir())
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


@pytest.mark.skipif(ipc.IS_WINDOWS, reason="POSIX permissions")
def test_bind_is_private_even_under_a_permissive_umask():
    """chmod-after-bind leaves a window; umask-around-bind does not."""
    old = os.umask(0)
    try:
        s = ipc.bind("default")
        mode = stat.S_IMODE(ipc.sock_path("default").stat().st_mode)
        assert mode & 0o077 == 0, f"socket is group/other accessible: {oct(mode)}"
    finally:
        os.umask(old)
        s.close()
        ipc.cleanup("default")


# --- round trip -------------------------------------------------------------

def test_request_response_round_trip():
    s = ipc.bind("default")
    if ipc.IS_WINDOWS:
        ipc.write_port("default", s.getsockname()[1], "tok")
    got: list = []
    _serve_once(s, {"pong": True, "pid": 42}, expect=got)
    sock, token = ipc.connect("default", timeout=5)
    try:
        reply = ipc.request(sock, token, {"meta": "ping"})
    finally:
        sock.close(); s.close(); ipc.cleanup("default")
    assert reply["pong"] is True and got[0]["meta"] == "ping"


def test_connect_without_a_daemon_raises_ipcerror_not_bare_oserror():
    with pytest.raises(ipc.IPCError):
        ipc.connect("nobody", timeout=0.5)


def test_reply_that_is_not_an_object_is_rejected():
    """A stale or hostile endpoint may reply with anything JSON-shaped."""
    s = ipc.bind("default")
    if ipc.IS_WINDOWS:
        ipc.write_port("default", s.getsockname()[1], "tok")
    _serve_once(s, [1, 2, 3])
    sock, token = ipc.connect("default", timeout=5)
    try:
        with pytest.raises(ipc.IPCError):
            ipc.request(sock, token, {"meta": "ping"})
    finally:
        sock.close(); s.close(); ipc.cleanup("default")


def test_closed_connection_without_reply_raises():
    s = ipc.bind("default")
    if ipc.IS_WINDOWS:
        ipc.write_port("default", s.getsockname()[1], "tok")

    def close_immediately():
        conn, _ = s.accept(); conn.close()
    threading.Thread(target=close_immediately, daemon=True).start()
    sock, token = ipc.connect("default", timeout=5)
    try:
        with pytest.raises(ipc.IPCError):
            ipc.request(sock, token, {"meta": "ping"})
    finally:
        sock.close(); s.close(); ipc.cleanup("default")


# --- ping is a handshake, not a bare connect (v1 #276, #161, #254) ----------

def test_ping_rejects_a_listener_that_is_not_our_daemon():
    """Port reuse after a crash: something is listening, but it is not us."""
    s = ipc.bind("default")
    if ipc.IS_WINDOWS:
        ipc.write_port("default", s.getsockname()[1], "tok")
    _serve_once(s, {"hello": "i am some other service"})
    try:
        assert ipc.ping("default", timeout=5) is None
    finally:
        s.close(); ipc.cleanup("default")


def test_ping_returns_the_reply_so_callers_can_check_browser_liveness():
    """'Alive' must mean the CDP socket too — a meta-only probe answers from a dict."""
    s = ipc.bind("default")
    if ipc.IS_WINDOWS:
        ipc.write_port("default", s.getsockname()[1], "tok")
    _serve_once(s, {"pong": True, "pid": 7, "browser_connected": False})
    try:
        reply = ipc.ping("default", timeout=5)
        assert reply["browser_connected"] is False
    finally:
        s.close(); ipc.cleanup("default")


def test_ping_on_a_dead_endpoint_is_none_not_an_exception():
    assert ipc.ping("nobody", timeout=0.5) is None


# --- windows port file ------------------------------------------------------

def test_port_file_roundtrip_is_atomic_and_typed():
    ipc.write_port("default", 51234, "abc123")
    assert ipc.read_port("default") == (51234, "abc123")
    ipc.cleanup("default")


def test_unreadable_port_file_degrades_to_none():
    ipc.port_path("default").write_text("{ not json")
    assert ipc.read_port("default") == (None, None)


def test_cleanup_is_idempotent():
    ipc.cleanup("default")
    ipc.cleanup("default")


def test_cleanup_never_raises_even_for_an_unrepresentable_path(monkeypatch, tmp_path):
    """Teardown must not be blocked by the same limit that blocks bind."""
    monkeypatch.setenv("BH_RUNTIME_DIR", str(tmp_path / ("z" * 80)))
    ipc.cleanup("default")          # must be silent


def test_every_transport_failure_is_typed_regardless_of_race_timing():
    """A peer closing mid-exchange must not leak BrokenPipeError/ConnectionResetError.

    Those are siblings of IPCError under OSError, not subclasses — leaking one forces
    callers back to catching OSError broadly, which is the untyped-error disease.
    Run repeatedly: which side of the race you land on is timing-dependent.
    """
    for _ in range(12):
        s = ipc.bind("default")
        if ipc.IS_WINDOWS:
            ipc.write_port("default", s.getsockname()[1], "tok")

        def slam(listener=s):          # bind per iteration, not by reference
            conn, _ = listener.accept()
            conn.close()
        threading.Thread(target=slam, daemon=True).start()
        sock, token = ipc.connect("default", timeout=5)
        try:
            with pytest.raises(ipc.IPCError):
                ipc.request(sock, token, {"meta": "ping"})
        finally:
            sock.close(); s.close(); ipc.cleanup("default")
