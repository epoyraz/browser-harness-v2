"""Doctor tests. The one that earns its keep: zero windows is NOT 'click Allow'."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from harness.connect.doctor import (
    GUIDANCE,
    count_pages,
    diagnose,
    render,
    to_json,
)
from harness.core.outcome import Class, ok


class _DevTools(BaseHTTPRequestHandler):
    def do_GET(self):
        cfg = self.server.cfg
        key = self.path.rsplit("/", 1)[-1]
        if key in cfg:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(cfg[key].encode())
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
        return f"http://127.0.0.1:{srv.server_port}"
    yield make
    for s in servers:
        s.shutdown()


def _env(url, tmp_path):
    return {"BU_CDP_URL": url, "BH_RUNTIME_DIR": "/tmp/bh-doctor-test",
            "BH_PROFILE_DIRS": str(tmp_path / "none")}


def _version():
    return json.dumps({"webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/browser/x"})


def _pages(n):
    return json.dumps([{"type": "page", "id": str(i)} for i in range(n)]
                      + [{"type": "service_worker", "id": "sw"}])


# --- classification ----------------------------------------------------------

def test_healthy_endpoint_reports_ok_with_its_strategy(serve, tmp_path):
    url = serve(version=_version(), list=_pages(2))
    out = diagnose("doc1", _env(url, tmp_path))
    assert out.ok and out.observed["strategy"] == "pinned"
    assert out.observed["pages"] == 2               # service workers not counted


def test_zero_windows_is_no_browser_window_not_click_allow(serve, tmp_path):
    """v1 mapped every handshake stall to 'click Allow' — including against a browser with
    zero windows, where no consent popup can exist. Rule 1: report what was observed."""
    url = serve(version=_version(), list=_pages(0))
    out = diagnose("doc2", _env(url, tmp_path))
    assert out.ok is False and out.cls is Class.NO_BROWSER_WINDOW
    assert "zero page targets" in out.detail


def test_m147_hidden_list_is_unknown_not_a_guess(serve, tmp_path):
    """/json/list 404s on the default profile. Unknown window count must stay unknown —
    neither 'no window' nor 'permission pending' was observed."""
    url = serve(version=_version())                 # list intentionally 404s
    out = diagnose("doc3", _env(url, tmp_path))
    assert out.ok is True
    assert out.observed["pages"] is None


def test_nothing_reachable_is_typed_and_carries_the_attempts(tmp_path):
    out = diagnose("doc4", {"BH_RUNTIME_DIR": "/tmp/bh-doctor-test",
                            "BH_PROFILE_DIRS": str(tmp_path / "none")})
    assert out.cls is Class.ENDPOINT_UNREACHABLE
    assert len(out.observed["attempts"]) == 3       # ws, http, one profile dir — all report


def test_count_pages_returns_none_on_any_failure(serve):
    assert count_pages(serve()) is None
    assert count_pages("http://127.0.0.1:9") is None


# --- rendering ---------------------------------------------------------------

def test_render_shows_every_verdict_and_the_next_step(tmp_path):
    out = diagnose("doc5", {"BH_RUNTIME_DIR": "/tmp/bh-doctor-test",
                            "BH_PROFILE_DIRS": str(tmp_path / "none")})
    text = "\n".join(render(out))
    assert "endpoint_unreachable" in text
    assert "declined" in text                       # the losers are the diagnosis
    assert "next:" in text


def test_every_guided_class_has_actionable_prose():
    for cls, advice in GUIDANCE.items():
        assert isinstance(cls, Class) and len(advice) > 20


def test_doctor_cli_exits_nonzero_and_prints_attempts_when_unreachable(tmp_path):
    import subprocess
    import sys
    r = subprocess.run(
        [sys.executable, "-c",
         ("import sys; sys.argv=['bh','--doctor']; "
          "from harness.cli.main import main; raise SystemExit(main())")],
        capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "BH_RUNTIME_DIR": "/tmp/bh-doctor-test",
             "BH_PROFILE_DIRS": str(tmp_path / "none"),
             "PYTHONPATH": "."},
    )
    assert r.returncode == 1
    assert "declined" in r.stdout


# --- machine-readable verdict ---------------------------------------------------

def _resolved(ws="ws://127.0.0.1:9222/devtools/browser/2f222744-30bb-4cc3-9fd4", **extra):
    return ok(None, ws_url=ws, strategy="profile", attempts=[
        {"strategy": "explicit-ws", "candidate": "BU_CDP_WS",
         "won": False, "reason": "not set"},
        {"strategy": "profile", "candidate": ws, "won": True},
    ], **extra)


@pytest.mark.parametrize(("env", "secrets"), [
    ({"BU_CDP_WS": "wss://cloud.example.com/session/SECRET-TOKEN?key=abc123",
      "BH_TRUST": "pinned"}, ["SECRET-TOKEN", "abc123"]),
    ({"BU_CDP_URL": "https://relay.example.com/t/TOKEN123/json",
      "BH_TRUST": "pinned"}, ["TOKEN123"]),
])
def test_a_failed_diagnosis_leaks_nothing_either(env, secrets):
    """The success path was redacted field by field, and the failure path put the same URL
    in `detail` as prose and in `observed.pinned` — neither of which was a named field.
    The guarantee is about the whole document, so it is enforced over the whole document."""
    blob = json.dumps(to_json(diagnose("leakprobe", env)))
    for secret in secrets:
        assert secret not in blob, f"{secret!r} survived into the JSON"
    assert "example.com" in blob                  # the host is still the diagnosis


def test_scrubbing_leaves_the_things_that_are_not_endpoints():
    """`chrome://inspect` is guidance, a profile path is evidence, and a variable name is
    the reason discovery declined. None of them names a credential."""
    payload = to_json(diagnose("leakprobe", {"BH_PROFILE_DIRS": "/nonexistent-profile"}))
    blob = json.dumps(payload)
    assert "chrome://inspect" in blob or "BU_CDP_WS" in blob


def test_json_reduces_the_endpoint_to_topology():
    """`render()` keeps the full URL because a terminal line is ephemeral and the URL is
    the diagnosis. JSON is piped into files and pasted into issues, which is the journal's
    exposure — and the ws path drives the browser for anyone who can reach the port."""
    payload = to_json(_resolved())
    assert payload["observed"]["ws_url"] == "ws://127.0.0.1:9222"
    assert "2f222744" not in json.dumps(payload)


def test_json_keeps_the_verdicts_that_are_the_diagnosis():
    """A declined attempt's candidate is a variable name or a profile path, not a URL.
    Redacting those would leave the report saying nothing about why discovery failed."""
    payload = to_json(_resolved())
    attempts = payload["observed"]["attempts"]
    assert attempts[0]["candidate"] == "BU_CDP_WS"
    assert attempts[0]["reason"] == "not set"
    assert attempts[1]["candidate"] == "ws://127.0.0.1:9222"


def test_json_carries_the_typed_class_and_ok_flag():
    payload = to_json(_resolved(pages=3))
    assert payload["ok"] is True and payload["class"] == "ok"
    assert payload["observed"]["pages"] == 3


def test_the_human_renderer_is_unchanged_and_still_shows_the_url():
    """The two modes differ on purpose; a test that only pinned the JSON would let the
    terminal output drift into redacting the thing it exists to show."""
    lines = "\n".join(render(_resolved()))
    assert "2f222744" in lines


def test_json_does_not_mutate_the_outcome_it_was_given():
    outcome = _resolved()
    to_json(outcome)
    assert outcome.observed["ws_url"].endswith("/devtools/browser/2f222744-30bb-4cc3-9fd4")
