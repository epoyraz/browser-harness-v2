"""The agent-facing surface. Runs against a real daemon on a real socket, fake browser
behind it — the point is the wiring, and a mocked session would prove none of it."""
import os
import threading
import time

import pytest

from harness.connect.daemon import Daemon
from harness.core.outcome import ScopeRefused
from harness.session import Session, run_script
from tests.fake_browser import FakeBrowser


@pytest.fixture
def served(monkeypatch):
    d = f"/tmp/bhs{os.getpid()}"
    os.makedirs(d, exist_ok=True)
    monkeypatch.setenv("BH_RUNTIME_DIR", d)
    browser = FakeBrowser("a", "b")
    daemon = Daemon("sesstest", browser).start()
    threading.Thread(target=daemon.serve_forever, daemon=True).start()
    yield browser, daemon
    daemon.stop()


@pytest.fixture
def session(served):
    s = Session("sesstest")
    yield s
    s.close()


# --- tabs ---------------------------------------------------------------------

def test_a_session_attaches_to_an_existing_page_without_being_told(session):
    assert session.tab().target_id in ("a", "b")


def test_the_current_tab_is_client_local_not_daemon_state(served):
    """#375's actual fix. Two sessions = two processes' worth of state; the daemon keeps
    no cursor, so neither can steal the other's tab."""
    a, b = Session("sesstest"), Session("sesstest")
    try:
        a.use_tab("a")
        b.use_tab("b")
        assert a.tab().target_id == "a" and b.tab().target_id == "b"
        a.use_tab("b")                       # a moves; b must not
        assert b.tab().target_id == "b"
    finally:
        a.close(); b.close()


def test_tabs_are_reused_not_rebuilt(session):
    assert session.tab("a") is session.tab("a")


def test_two_threads_racing_one_target_construct_one_tab(session, monkeypatch):
    created = []
    created_lock = threading.Lock()

    class SlowTab:
        def __init__(self, conn, registry, target_id, **kwargs):
            with created_lock:
                created.append(target_id)
            time.sleep(0.05)
            self.target_id = target_id

        def close(self):
            pass

    monkeypatch.setattr("harness.session.Tab", SlowTab)
    start = threading.Barrier(8, timeout=5)
    tabs = []

    def attach():
        start.wait()
        tabs.append(session.tab("a"))

    threads = [threading.Thread(target=attach) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert created == ["a"]
    assert len({id(tab) for tab in tabs}) == 1


def test_switching_tabs_redirects_the_bare_helpers(session):
    """Helpers are late-bound, so a script reads top to bottom: use_tab() mid-script
    changes what goto()/js() act on."""
    ns = session.namespace()
    session.use_tab("a")
    assert ns["js"]("x") == "a"              # the fake echoes its target
    session.use_tab("b")
    assert ns["js"]("x") == "b"


def test_only_drivable_targets_are_auto_selected(served):
    """A chrome:// internal is never what a caller meant, so it is not seized as 'the tab'."""
    browser, _ = served
    browser.targets["dev"] = {"targetId": "dev", "type": "page", "url": "chrome://inspect/"}
    s = Session("sesstest")
    try:
        assert s.tab().target_id != "dev"
    finally:
        s.close()


def test_owned_browser_context_scopes_and_disposes_its_tabs(session, served):
    browser, _ = served
    context_id = session.new_context()
    tab = session.new_tab(context_id=context_id)
    assert browser.targets[tab.target_id]["browserContextId"] == context_id
    session.close_context(context_id)
    assert context_id not in browser.contexts
    assert tab.target_id not in browser.targets


def test_session_refuses_to_use_or_dispose_an_unowned_context(session):
    with pytest.raises(ScopeRefused):
        session.new_tab(context_id="someone-elses")
    with pytest.raises(ScopeRefused):
        session.close_context("someone-elses")


def test_new_tab_rolls_back_target_when_attach_fails(session, served, monkeypatch):
    browser, _ = served
    original = session.tab

    def fail_new(target_id=None):
        if target_id and target_id.startswith("T"):
            raise RuntimeError("attach failed")
        return original(target_id)

    monkeypatch.setattr(session, "tab", fail_new)
    before = set(browser.targets)
    with pytest.raises(RuntimeError, match="attach failed"):
        session.new_tab()
    assert set(browser.targets) == before


def test_new_tab_rolls_back_target_when_navigation_fails(session, served):
    browser, _ = served
    browser.navigate_error = "net::ERR_FAILED"
    before = set(browser.targets)
    with pytest.raises(Exception, match="navigation_failed"):
        session.new_tab("https://broken.test")
    assert set(browser.targets) == before


# --- namespace ----------------------------------------------------------------

def test_the_namespace_covers_the_documented_surface(session):
    ns = session.namespace()
    for name in ("goto", "js", "cdp", "snapshot", "click_ref", "click_at", "page_text",
                 "press_key", "scroll", "upload_file", "capture_screenshot",
                 "wait_lifecycle", "form_schema", "fill_form", "set_value",
                 "require_form", "prepare_application", "fetch_all", "new_tab", "use_tab", "close_tab",
                 "new_context", "close_context", "parallel", "summarise", "targets",
                 "tab", "session", "journal"):
        assert name in ns, f"SKILL.md documents {name}() but the namespace lacks it"


def test_prepare_application_stops_before_frame_discovery_when_main_is_a_form(session, served):
    browser, _ = served
    payload = {"schema": {"verdict": {"is_form": True}, "fields": [{"ref": "e1"}]},
               "url": "https://a.test/apply", "title": "Apply", "language": "en",
               "file_inputs": [], "apply_link": None}
    browser.eval_hook = lambda expression: (
        True if "__bhDryRunGuardInstalled" in expression else payload)
    prepared = session.prepare_application()
    assert prepared["context"] == "main" and prepared["contexts_checked"] == 1
    assert prepared["is_application"] is True
    assert not any(call.get("method") == "Target.setAutoAttach" for call in browser.calls)


def test_prepare_application_stops_for_a_substantial_js_button_form(session, served):
    browser, _ = served
    payload = {"schema": {"verdict": {"is_form": False},
                          "fields": [{"ref": f"e{i}"} for i in range(8)]},
               "url": "https://a.test/apply", "title": "Apply", "language": "en",
               "file_inputs": [], "apply_link": None}
    browser.eval_hook = lambda expression: (
        True if "__bhDryRunGuardInstalled" in expression else payload)
    prepared = session.prepare_application()
    assert prepared["contexts_checked"] == 1 and prepared["is_application"] is True
    assert not any(call.get("method") == "Target.setAutoAttach" for call in browser.calls)


def test_helpers_keep_their_names_so_a_traceback_is_readable(session):
    ns = session.namespace()
    assert ns["goto"].__name__ == "goto" and ns["fill_form"].__name__ == "fill_form"


# --- run_script ---------------------------------------------------------------

def test_a_script_runs_against_the_session(served, capsys):
    assert run_script('print(js("hi"))', name="sesstest") == 0
    assert capsys.readouterr().out.strip() == "a"


def test_bh_argv_does_not_leak_into_the_script(served, capsys):
    """Found live: a script read `sys.argv[1]`, got bh's own `-` flag, and asked the daemon
    to attach to a target named '-'. The daemon answered `target_gone` correctly — but the
    script should never have seen that argv."""
    import sys
    before = list(sys.argv)
    assert run_script("import sys; print(sys.argv)", name="sesstest") == 0
    assert capsys.readouterr().out.strip() == "['<bh>']"
    assert sys.argv == before                # and it is restored, not clobbered


def test_a_typed_failure_prints_an_outcome_and_exits_1(served, capsys):
    code = run_script('use_tab("ghosttab"); js("x")', name="sesstest")
    err = capsys.readouterr().err
    assert code == 1
    assert '"class"' in err and "Traceback" not in err


def test_a_plain_python_error_is_not_swallowed(served):
    """Only harness failures get the outcome treatment; a bug in the agent's own script
    must still surface as the exception it is."""
    with pytest.raises(ZeroDivisionError):
        run_script("1/0", name="sesstest")
