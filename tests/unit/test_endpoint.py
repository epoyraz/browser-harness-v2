"""Endpoint tests, against real sockets and real HTTP servers — the failure modes here
(refused, stale, squatted-on port, 404) are transport behaviours, not library behaviours."""
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from harness.connect import endpoint
from harness.connect.endpoint import (
    Binding,
    BrowserIdentity,
    binding_for,
    browser_identity,
    discover,
    probe_http,
    profile_dirs,
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


def test_macos_identity_binds_a_known_profile_to_the_unique_listener(monkeypatch):
    monkeypatch.setattr(endpoint.sys, "platform", "darwin")
    monkeypatch.setattr(endpoint, "mac_listener_pid", lambda ws: 7391)
    profile = Path.home() / "Library/Application Support/BraveSoftware/Brave-Browser"

    identity = browser_identity("ws://127.0.0.1:9222/devtools/browser/x", profile)

    assert identity == BrowserIdentity(
        pid=7391, application="Brave Browser", profile_dir=str(profile),
        ws_url="ws://127.0.0.1:9222/devtools/browser/x")


def test_macos_listener_identity_never_guesses_between_multiple_pids(monkeypatch):
    monkeypatch.setattr(endpoint.sys, "platform", "darwin")
    completed = type("Completed", (), {
        "returncode": 0, "stdout": "p101\np202\n", "stderr": "",
    })()
    monkeypatch.setattr(endpoint.subprocess, "run", lambda *a, **kw: completed)

    assert endpoint.mac_listener_pid(
        "ws://127.0.0.1:9222/devtools/browser/x") is None


@pytest.mark.parametrize("profile_count", [0, 2])
def test_macos_explicit_endpoint_profile_matching_fails_closed(
        tmp_path, monkeypatch, profile_count):
    monkeypatch.setattr(endpoint.sys, "platform", "darwin")
    monkeypatch.setattr(endpoint, "mac_listener_pid", lambda ws: 7391)
    monkeypatch.setattr(endpoint, "_mac_process_name", lambda pid: "Google Chrome")
    profiles = [tmp_path / f"profile-{index}" for index in range(profile_count)]
    for profile in profiles:
        profile.mkdir()
        (profile / "DevToolsActivePort").write_text(
            "9222\n/devtools/browser/same", encoding="utf-8")

    configured = profiles or [tmp_path / "missing"]
    identity = browser_identity(
        "ws://127.0.0.1:9222/devtools/browser/same",
        env={"BH_PROFILE_DIRS": os.pathsep.join(map(str, configured))},
    )

    assert identity == BrowserIdentity(
        pid=7391, application="Google Chrome", profile_dir="",
        ws_url="ws://127.0.0.1:9222/devtools/browser/same")


def test_a_persisted_local_pin_recovers_its_exact_profile_on_respawn(
        runtime, tmp_path, monkeypatch):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    path = "/devtools/browser/persisted"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)   # not $HOME: see fake_home
    profile = tmp_path / "Library/Application Support/Google/Chrome"
    profile.mkdir(parents=True)
    (profile / "DevToolsActivePort").write_text(f"{port}\n{path}", encoding="utf-8")
    ws_url = f"ws://127.0.0.1:{port}{path}"
    monkeypatch.setattr(endpoint.sys, "platform", "darwin")
    monkeypatch.setattr(endpoint, "mac_listener_pid", lambda ws: 8844)
    env = {"BU_CDP_WS": ws_url}

    try:
        binding_for("identity-respawn", env)
        respawned = binding_for("identity-respawn", {})
        resolution = resolve(respawned, {})
    finally:
        listener.close()

    assert resolution.identity == BrowserIdentity(
        pid=8844, application="Google Chrome", profile_dir=str(profile), ws_url=ws_url)


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
    """Both profile dirs are absent, so they arrive as one rollup rather than two lines —
    but the rollup still names each of them, so no verdict is lost."""
    a, b = tmp_path / "a", tmp_path / "b"
    with pytest.raises(EndpointUnreachable) as e:
        discover({"BH_PROFILE_DIRS": os.pathsep.join((str(a), str(b))),
                  "BU_CDP_URL": f"http://127.0.0.1:{_closed_port()}"})
    attempts = e.value.observed["attempts"]
    assert len(attempts) == 3                       # ws, http, and the absent-dirs rollup
    assert all(not a_["won"] for a_ in attempts)
    rollup = attempts[-1]
    # The rollup names paths in the home-relative form `_tilde` promises, so a tmp_path
    # that happens to live under the home directory (Windows: %LOCALAPPDATA%\Temp) shows
    # up as `~/...`. Assert the form the code documents, not the absolute string.
    assert endpoint._tilde(a) in rollup["reason"] and endpoint._tilde(b) in rollup["reason"]


# --- the profile table (the out-of-the-box case: find the browser v1 found) ---

MAC_TABLE = [
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
    "Library/Application Support/Chromium",
]
LINUX_TABLE = [
    ".config/google-chrome",
    ".config/chromium",
    ".config/chromium-browser",
    ".config/microsoft-edge",
    ".config/microsoft-edge-beta",
    ".config/microsoft-edge-dev",
    ".var/app/org.chromium.Chromium/config/chromium",
    ".var/app/com.google.Chrome/config/google-chrome",
    ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser",
    ".var/app/com.microsoft.Edge/config/microsoft-edge",
]
WINDOWS_TABLE = [
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
]


@pytest.fixture
def fake_home(monkeypatch):
    """Pin `Path.home()` itself. Setting `$HOME` only steers it on POSIX — Windows reads
    `%USERPROFILE%` and ignored the variable, so five tests here were red on every Windows
    run since they were written while proving nothing about the code."""
    # `.resolve()` makes the fake home absolute *on the host*: "/home/tester" is absolute on
    # POSIX and drive-less on Windows, where every darwin/linux candidate built on it then
    # failed `is_absolute()` — a claim about the test's own fixture, not about the code.
    home = Path("/home/tester").resolve()
    monkeypatch.setattr(Path, "home", lambda: home)
    # `Path.expanduser()` does not go through `Path.home()`: it reads $HOME on POSIX and
    # %USERPROFILE% on Windows. `BH_PROFILE_DIRS=~/one` needs both to land in the same place.
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def test_macos_probes_every_v1_vendor_path(monkeypatch, fake_home):
    """v1 looked in ten places; v2's first cut looked in two, so a Brave/Edge/Arc/Canary
    user simply got 'endpoint unreachable'. The whole table is the contract."""
    monkeypatch.setattr(sys, "platform", "darwin")
    assert profile_dirs({}) == [fake_home / p for p in MAC_TABLE]


def test_linux_probes_every_v1_vendor_path_including_flatpak(monkeypatch, fake_home):
    """The .var/app entries are the ones that matter most: a Flatpak-only desktop has no
    ~/.config/google-chrome at all and looked browserless without them."""
    monkeypatch.setattr(sys, "platform", "linux")
    assert profile_dirs({}) == [fake_home / p for p in LINUX_TABLE]


def test_windows_probes_every_v1_vendor_path_under_localappdata(monkeypatch, fake_home):
    monkeypatch.setattr(sys, "platform", "win32")
    dirs = profile_dirs({"LOCALAPPDATA": "/local/app/data"})
    assert dirs == [Path("/local/app/data") / p for p in WINDOWS_TABLE]


def test_windows_falls_back_to_home_appdata_when_localappdata_is_missing(
        monkeypatch, fake_home):
    """A stripped service env has no %LOCALAPPDATA%; the old code fell back to bare ~,
    which points at no profile that has ever existed."""
    monkeypatch.setattr(sys, "platform", "win32")
    assert profile_dirs({})[0] == fake_home / "AppData/Local/Google/Chrome/User Data"


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_mainstream_chrome_is_probed_first_on_every_platform(monkeypatch, fake_home,
                                                             platform):
    """Order is probe order. Widening the table must not make the common case pay for the
    long tail behind it."""
    monkeypatch.setattr(sys, "platform", platform)
    first = profile_dirs({"LOCALAPPDATA": "/local"})[0]
    assert "Chrome" in str(first) or "chrome" in str(first)
    assert "Canary" not in str(first) and "Beta" not in str(first)


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_every_candidate_is_absolute_and_has_no_unexpanded_tilde(monkeypatch, fake_home, tmp_path,
                                                                 platform):
    monkeypatch.setattr(sys, "platform", platform)
    # An absolute path *for the host*: "/local" is absolute on POSIX and drive-less — so
    # relative — on Windows, which failed the assertion below on every Windows run.
    dirs = profile_dirs({"LOCALAPPDATA": str(tmp_path / "local")})
    assert len(dirs) >= 10
    assert all(d.is_absolute() for d in dirs)
    assert not any("~" in str(d) for d in dirs)


def test_bh_profile_dirs_still_replaces_the_table_entirely(monkeypatch, fake_home):
    """The override is a replacement, not an addition: someone driving a throwaway
    --user-data-dir must not also get their daily-driver Chrome offered up (#479)."""
    monkeypatch.setattr(sys, "platform", "darwin")
    dirs = profile_dirs({"BH_PROFILE_DIRS": os.pathsep.join(("~/one", "/two"))})
    assert dirs == [fake_home / "one", Path("/two")]


def test_absent_profile_dirs_collapse_into_one_readable_attempt(monkeypatch, fake_home,
                                                                tmp_path):
    """Eleven identical 'no DevToolsActivePort file' lines is worse UX than two. The
    uninstalled browsers roll up; the installed-but-not-debugging one keeps its own line."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / "Library/Application Support/Google/Chrome").mkdir(parents=True)
    with pytest.raises(EndpointUnreachable) as e:
        discover({})
    profile = [a for a in e.value.observed["attempts"] if a["strategy"] == "profile"]
    assert len(profile) == 2                        # the real Chrome line + one rollup
    assert "not running with remote debugging" in profile[0]["reason"]
    assert "not present" in profile[1]["candidate"]
    assert "Brave-Browser" in profile[1]["reason"]  # the rollup still names every path


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


# --- what a shared journal is allowed to say about an endpoint -----------------

#: Each case pairs a URL with the substrings that must not survive into a log line.
CREDENTIALED_ENDPOINTS = [
    ("ws://127.0.0.1:9222/devtools/browser/2f222744-30bb-4cc3-9fd4-ba264d552e73",
     ["2f222744", "devtools"]),
    ("wss://cloud.example.com/session/SECRET-TOKEN?key=abc123",
     ["SECRET-TOKEN", "abc123", "session"]),
    ("ws://[::1]:9222/devtools/browser/deadbeef", ["deadbeef", "devtools"]),
]


@pytest.mark.parametrize(("url", "secrets"), CREDENTIALED_ENDPOINTS)
def test_an_endpoint_label_carries_no_means_of_reaching_it(url, secrets):
    """A CDP websocket URL is a bearer capability, not an address: the browser GUID drives
    the browser for anyone who can reach the port, and a remote endpoint can carry a
    provider token in the path or query. `bh --doctor` printed one of these into a
    transcript before this existed."""
    label = endpoint.safe_endpoint(url)
    for secret in secrets:
        assert secret not in label, f"{secret!r} survived into {label!r}"
    assert "?" not in label and label.count("/") == 2      # scheme separator only


@pytest.mark.parametrize(("url", "expected"), [
    ("ws://127.0.0.1:9222/devtools/browser/x", "ws://127.0.0.1:9222"),
    ("wss://cloud.example.com/session/tok", "wss://cloud.example.com"),
    ("ws://[::1]:9222/devtools/browser/x", "ws://[::1]:9222"),
    ("ws://host-no-port/devtools/browser/x", "ws://host-no-port"),
])
def test_the_label_still_says_which_browser_was_found(url, expected):
    """Redaction that also destroys the diagnosis is not a fix — scheme, host and port are
    what answers "which browser did it resolve"."""
    assert endpoint.safe_endpoint(url) == expected


@pytest.mark.parametrize("url", ["", "not a url", "ws://h:99999999/x", "///"])
def test_an_unparseable_endpoint_redacts_rather_than_leaks(url):
    """Including a port that raises on access — failing open would print the raw URL."""
    assert endpoint.safe_endpoint(url) == "<redacted-endpoint>"


def test_the_daemon_journals_the_label_and_not_the_url():
    """The contract `telemetry.py` states is that URLs are never selected into the journal
    in the first place. This is the one line that was breaking it."""
    import inspect

    from harness.connect import daemon
    source = inspect.getsource(daemon.serve)
    assert "ws=safe_endpoint(" in source
    assert "ws=resolution.ws_url" not in source
