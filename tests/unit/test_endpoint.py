"""Endpoint tests, against real sockets and real HTTP servers — the failure modes here
(refused, stale, squatted-on port, 404) are transport behaviours, not library behaviours."""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from harness.connect.endpoint import (
    Binding,
    binding_for,
    discover,
    probe_http,
    read_active_port,
    resolve,
)
from harness.core.outcome import (
    Endpoint404,
    EndpointUnreachable,
    ScopeRefused,
)


class _DevTools(BaseHTTPRequestHandler):
    def do_GET(self):
        cfg = self.server.cfg
        if self.path == "/json/version" and "version" in cfg:
            body = cfg["version"].encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *a):
        pass


@pytest.fixture
def serve():
    servers = []

    def make(**cfg):
        srv = HTTPServer(("127.0.0.1", 0), _DevTools)
        srv.cfg = cfg
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_port}", srv.server_port
    yield make
    for s in servers:
        s.shutdown()


def _version_body(port):
    return json.dumps({"Browser": "Chrome/147.0", "webSocketDebuggerUrl":
                       f"ws://127.0.0.1:{port}/devtools/browser/abc"})


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def runtime(monkeypatch, tmp_path_factory):
    import os
    d = f"/tmp/bhe{os.getpid()}"
    os.makedirs(d, exist_ok=True)
    monkeypatch.setenv("BH_RUNTIME_DIR", d)
    yield d
    for f in os.listdir(d):
        os.unlink(os.path.join(d, f))


# --- probes ------------------------------------------------------------------

def test_probe_returns_the_ws_url(serve):
    url, _ = serve(version=_version_body(0))
    assert probe_http(url)["ws"].startswith("ws://127.0.0.1:")


def test_a_404_is_endpoint_404_not_unreachable(serve):
    """M147: Chrome is *up*, its HTTP surface is locked. Conflating this with 'no browser'
    sends the user to start a Chrome that is already running."""
    url, _ = serve()
    with pytest.raises(Endpoint404) as e:
        probe_http(url)
    assert e.value.observed["status"] == 404


def test_a_refused_port_is_unreachable(serve):
    with pytest.raises(EndpointUnreachable):
        probe_http(f"http://127.0.0.1:{_closed_port()}")


def test_a_squatter_on_the_port_is_not_called_a_browser(serve):
    """The stale-port trap: after a crash, any process may hold the recorded port."""
    url, _ = serve(version="<html>hello i am a dev server</html>")
    with pytest.raises(EndpointUnreachable) as e:
        probe_http(url)
    assert "not as a DevTools endpoint" in e.value.args[0]


def test_active_port_file_parses_and_rejects_garbage(tmp_path):
    (tmp_path / "DevToolsActivePort").write_text("9222\n/devtools/browser/xyz")
    assert read_active_port(tmp_path) == (9222, "/devtools/browser/xyz")
    (tmp_path / "DevToolsActivePort").write_text("not a port\nwhatever")
    assert read_active_port(tmp_path) is None
    assert read_active_port(tmp_path / "missing") is None


# --- discovery (TODO 11's done-when: winners and losers both report) ---------

def test_explicit_url_wins_and_the_losers_say_why(serve, tmp_path):
    url, _ = serve(version=_version_body(0))
    r = discover({"BU_CDP_URL": url, "BH_PROFILE_DIRS": str(tmp_path / "nope")})
    assert r.strategy == "explicit-http" and r.ws_url.startswith("ws://")
    by = {a.strategy: a for a in r.attempts}
    assert by["explicit-http"].won
    assert by["explicit-ws"].reason == "not set"          # every strategy reports


def test_profile_strategy_needs_no_http_at_all(tmp_path):
    """M147-proof: DevToolsActivePort gives port + ws path directly."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    (tmp_path / "DevToolsActivePort").write_text(f"{port}\n/devtools/browser/xyz")
    try:
        r = discover({"BH_PROFILE_DIRS": str(tmp_path)})
        assert r.ws_url == f"ws://127.0.0.1:{port}/devtools/browser/xyz"
        assert r.strategy == "profile"
    finally:
        listener.close()


def test_a_stale_active_port_file_is_named_stale(tmp_path):
    (tmp_path / "DevToolsActivePort").write_text(f"{_closed_port()}\n/devtools/browser/x")
    with pytest.raises(EndpointUnreachable) as e:
        discover({"BH_PROFILE_DIRS": str(tmp_path)})
    reasons = [a["reason"] for a in e.value.observed["attempts"] if a.get("reason")]
    assert any("stale" in r for r in reasons)


def test_total_failure_carries_every_verdict(tmp_path):
    with pytest.raises(EndpointUnreachable) as e:
        discover({"BH_PROFILE_DIRS": str(tmp_path / "a") + ":" + str(tmp_path / "b"),
                  "BU_CDP_URL": f"http://127.0.0.1:{_closed_port()}"})
    attempts = e.value.observed["attempts"]
    assert len(attempts) == 4                       # ws, http, and both profile dirs
    assert all(not a["won"] for a in attempts)


# --- binding (TODO 12: pinned never widens scope) ----------------------------

def test_an_explicit_env_url_is_a_pin():
    assert Binding.from_env({"BU_CDP_URL": "http://127.0.0.1:1"}).mode == "pinned"
    assert Binding.from_env({"BU_CDP_WS": "ws://127.0.0.1:1/x"}).mode == "pinned"
    assert Binding.from_env({}).mode == "discover"


def test_pinned_and_dead_refuses_even_with_a_live_browser_discoverable(tmp_path, serve):
    """#479, the test that matters: the pinned browser died, the user's daily-driver Chrome
    is right there and discoverable — and we must not touch it."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)                              # a live, discoverable "daily driver"
    (tmp_path / "DevToolsActivePort").write_text(
        f"{listener.getsockname()[1]}\n/devtools/browser/daily")
    try:
        dead = f"ws://127.0.0.1:{_closed_port()}/devtools/browser/pinned"
        with pytest.raises(EndpointUnreachable) as e:
            resolve(Binding("pinned", dead), {"BH_PROFILE_DIRS": str(tmp_path)})
        assert e.value.observed["pinned"] == dead   # names the pin, offers no substitute
        assert e.value.retryable is False
    finally:
        listener.close()


def test_a_respawn_without_env_stays_pinned(runtime, tmp_path):
    """#479's root cause: the daemon was respawned without its env and silently became a
    discoverer. The pin is persisted per daemon name, so the respawn finds it."""
    binding_for("r7k2", {"BU_CDP_WS": "ws://127.0.0.1:1/devtools/browser/x"})
    respawned = binding_for("r7k2", {})             # clean env, as a supervisor would give
    assert respawned.mode == "pinned"
    assert respawned.url == "ws://127.0.0.1:1/devtools/browser/x"


def test_trust_discover_is_the_deliberate_way_out(runtime):
    binding_for("r7k3", {"BU_CDP_WS": "ws://127.0.0.1:1/x"})
    assert binding_for("r7k3", {"BH_TRUST": "discover"}).mode == "discover"
    assert binding_for("r7k3", {}).mode == "discover"      # and the pin stays cleared


def test_pinned_with_no_endpoint_is_scope_refused(runtime):
    with pytest.raises(ScopeRefused):
        resolve(Binding("pinned", ""), {})


def test_a_corrupt_binding_file_does_not_crash_loading(runtime):
    from harness.core import ipc
    (ipc.runtime_dir() / "bad.binding").write_text("{not json")
    assert Binding.load("bad") is None
