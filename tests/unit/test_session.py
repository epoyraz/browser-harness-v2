"""The agent-facing surface. Runs against a real daemon on a real socket, fake browser
behind it — the point is the wiring, and a mocked session would prove none of it."""
import json
import threading
import time

import pytest

from harness.core.outcome import ScopeRefused
from harness.session import Session, run_script

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


def test_two_fresh_clients_do_not_adopt_the_same_page(served):
    """The multi-client startup collision, which is browser-use PR 618's bug one layer
    down: the no-target fallback was computed client-side (list drivable pages, take the
    first), so two fresh clients against one browser both took the SAME page and their
    navigations clobbered each other. The daemon now hands each connected client the
    first page nobody else has adopted — atomically — and creates a background tab once
    every page is spoken for."""
    browser, _ = served
    a, b, c = Session("sesstest"), Session("sesstest"), Session("sesstest")
    try:
        picks = [a.tab().target_id, b.tab().target_id, c.tab().target_id]
        assert len(set(picks)) == 3, picks           # three clients, three tabs
        assert set(picks[:2]) == {"a", "b"}          # existing pages first, in order
        creates = [call["params"] for call in browser.calls
                   if call.get("method") == "Target.createTarget"]
        assert creates == [{"url": "about:blank", "background": True}]
    finally:
        a.close(); b.close(); c.close()


def test_a_closed_clients_adoption_returns_to_the_pool(served):
    """Adoption is connection-scoped, not a lease: no release call exists or is needed.
    When the client goes, its default tab becomes adoptable again — and the TAB stays
    open, because closing it is close_tab's job, never implied."""
    first = Session("sesstest")
    assert first.tab().target_id == "a"
    first.close()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:               # the daemon reaps the peer async
        replacement = Session("sesstest")
        try:
            if replacement.tab().target_id == "a":
                return
        finally:
            replacement.close()
        time.sleep(0.05)
    raise AssertionError("the first client's adoption was never released")


def test_adoption_never_restricts_an_explicit_target(served):
    """Adoption governs only the ergonomic no-target fallback. A caller that NAMES a
    target gets it, adopted elsewhere or not — two collaborating clients on one tab is
    a legitimate arrangement, and refusing it would turn advice into a lock."""
    owner, guest = Session("sesstest"), Session("sesstest")
    try:
        assert owner.tab().target_id == "a"          # owner adopts it...
        assert guest.use_tab("a").target_id == "a"   # ...guest may still name it
    finally:
        owner.close(); guest.close()


def _title_calls(browser):
    # The marker travels as JS surrogate escapes (🐴), so match the escape —
    # the Python-side expression string never contains the raw emoji.
    return [c for c in browser.calls
            if c.get("method") == "Runtime.evaluate"
            and "udc34" in str((c.get("params") or {}).get("expression", ""))]


def test_the_tab_marker_is_off_unless_asked_for(session, served):
    """`document.title` is page-visible state — analytics read it — and the
    detectability contract is that the harness announces nothing to the page unless the
    operator asks. So the marker is strictly opt-in."""
    browser, _ = served
    session.use_tab("a")
    session.use_tab("b")
    assert _title_calls(browser) == []


def test_the_tab_marker_follows_the_cursor_when_enabled(served, monkeypatch):
    """BH_TAB_MARK=1: the driven tab carries the 🐴 prefix (browser-use's convention), so
    a human watching ten hidden worker tabs can tell the harness's tabs from their own at
    a glance. Moving the cursor marks the new tab and unmarks the one it left."""
    monkeypatch.setenv("BH_TAB_MARK", "1")
    browser, _ = served
    s = Session("sesstest")
    try:
        s.use_tab("a")
        marks = _title_calls(browser)
        assert marks, "attaching never marked the tab"
        s.use_tab("b")
        by_session = [(browser.sessions.get(c.get("sessionId")),
                       "slice(3)" in c["params"]["expression"]) for c in _title_calls(browser)]
        assert ("b", False) in by_session          # the new current tab was marked
        assert ("a", True) in by_session           # the abandoned one was unmarked
        n = len(_title_calls(browser))
        s.tab()                                    # cursor unchanged -> no re-mark
        assert len(_title_calls(browser)) == n
    finally:
        s.close()


def test_closing_a_dirty_tab_answers_its_own_leave_site_prompt(served):
    """Chrome raises the beforeunload dialog when a tab whose page armed it is CLOSED,
    and blocks Target.closeTarget until the dialog is answered. close_tab used to tear
    down the local Tab — unsubscribing the only dialog listener — before issuing the
    close, so the prompt went unanswered and the close wedged. This is the parallel()
    cleanup path: the tab being closed is one that was just FILLED, which is exactly the
    page that armed the handler. The fake models Chrome faithfully: closeTarget answers
    only after handleJavaScriptDialog arrives."""
    browser, _ = served
    s = Session("sesstest")
    try:
        tab = s.use_tab("a")
        released = threading.Event()
        real_send = browser.send

        def chrome_like(msg):
            method = msg.get("method")
            if method == "Target.closeTarget":
                browser.emit("Page.javascriptDialogOpening",
                             {"type": "beforeunload", "message": "Leave site?"},
                             session_id=tab._session_id)

                def answer_when_released(deferred=msg):
                    if released.wait(5):
                        real_send(deferred)     # the dialog was answered; close proceeds

                threading.Thread(target=answer_when_released, daemon=True).start()
                return
            if method == "Page.handleJavaScriptDialog":
                released.set()
            real_send(msg)

        browser.send = chrome_like
        s.close_tab("a", wait=False)            # completes instead of wedging
        handled = [c for c in browser.calls
                   if c.get("method") == "Page.handleJavaScriptDialog"]
        assert handled and handled[0]["params"]["accept"] is True
    finally:
        browser.send = real_send
        s.close()


def test_new_tab_is_created_in_the_background(session, served):
    """`Target.createTarget` defaults to foreground. Measured on four consecutive
    creations: the user's selected tab loses focus once per tab, and afterwards exactly
    one harness tab — whichever was created LAST — can receive raw Input.* events while
    the rest silently drop them. Background creation removes the focus theft and makes
    every worker tab the same, deterministic, handled case."""
    browser, _ = served
    session.new_tab()
    creates = [c["params"] for c in browser.calls
               if c.get("method") == "Target.createTarget"]
    assert creates and all(p.get("background") is True for p in creates)


def test_activate_tab_is_the_explicit_visibility_opt_in(session, served):
    """Everything else works hidden; activation exists for the page that pauses
    visibility-dependent rendering, and for the human who wants to watch. It must be a
    deliberate call — never a side effect of attaching."""
    browser, _ = served
    session.use_tab("a")
    before = [c for c in browser.calls if c.get("method") == "Target.activateTarget"]
    assert before == []                       # attaching alone never activated anything
    tid = session.activate_tab()
    activates = [c["params"] for c in browser.calls
                 if c.get("method") == "Target.activateTarget"]
    assert tid == "a" and activates == [{"targetId": "a"}]


def test_a_fresh_client_can_resume_an_explicit_daemon_owned_target_lease(served):
    owner = Session("sesstest")
    try:
        owner.use_tab("b")
        lease = owner.lease_tab()
        # A fresh process would otherwise pick a first listed page ("a").  Claiming the
        # lease binds it to b without reintroducing a daemon-wide current-tab cursor.
        fresh = Session("sesstest")
        try:
            assert fresh.resume_lease(lease).target_id == "b"
            assert fresh.tab().target_id == "b"
        finally:
            fresh.close()
    finally:
        owner.close()


def test_implicit_adoption_never_takes_an_actively_leased_target(served):
    """A lease reserves a target across fresh processes. The ergonomic no-target path
    must therefore treat it as unavailable, even when no peer has adopted it yet."""
    owner = Session("sesstest")
    lease = owner.lease_tab("a")
    fresh = Session("sesstest")
    try:
        assert fresh.tab().target_id == "b"
    finally:
        fresh.close()
        owner.release_lease(lease)
        owner.close()


def test_a_target_cannot_be_leased_twice_until_released(served):
    owner, other = Session("sesstest"), Session("sesstest")
    try:
        lease = owner.lease_tab("a")
        with pytest.raises(ScopeRefused, match="already has an active lease"):
            other.lease_tab("a")
        owner.release_lease(lease)
        assert other.lease_tab("a")
    finally:
        owner.close(); other.close()


def test_an_invalid_environment_lease_fails_closed_instead_of_picking_a_tab(served, monkeypatch):
    monkeypatch.setenv("BH_TARGET_LEASE", "not-a-real-lease")
    with pytest.raises(ScopeRefused, match="unknown or expired target lease"):
        Session("sesstest")


def test_a_destroyed_target_expires_its_lease(served):
    browser, _ = served
    owner = Session("sesstest")
    try:
        lease = owner.lease_tab("a")
        browser.destroy("a")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                owner.resume_lease(lease)
            except ScopeRefused:
                break
            time.sleep(0.01)
        else:
            pytest.fail("destroyed target lease remained claimable")
    finally:
        owner.close()


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
    """The core surface only. The application workflow moved to `applications/`, which
    binds itself into a script namespace through `install()` — so a name missing here is
    a core regression, and a name appearing here that belongs to a domain is the boundary
    leaking back."""
    ns = session.namespace()
    for name in ("goto", "open_page", "read_page", "js", "cdp", "snapshot",
                 "click_ref", "click_at", "page_text",
                 "press_key", "scroll", "upload_file", "capture_screenshot",
                 "wait_lifecycle", "form_schema", "fill_form",
                 "start_diagnostics", "diagnostics",
                 "set_value", "require_form",
                 "fetch_all",
                 "open_pages", "new_tab",
                 "use_tab", "close_tab",
                 "lease_tab", "resume_lease", "release_lease",
                 "new_context", "close_context", "parallel", "summarise", "targets",
                 "fetch_content", "tab", "session", "journal"):
        assert name in ns, f"SKILL.md documents {name}() but the namespace lacks it"
    for name in ("prepare_application", "follow_application", "locate_application",
                 "run_application", "application_skills", "wait_for_application_state"):
        assert name not in ns, f"{name}() is application judgment; core must not carry it"


def test_open_pages_divides_one_text_budget_across_the_batch(session, monkeypatch):
    calls = []

    class Page:
        def open_page(self, url, **kwargs):
            calls.append((url, kwargs))
            return {
                "landed": url,
                "lifecycle": "load",
                "page": {
                    "title": url,
                    "text": "body",
                    "links": [],
                    "text_truncated": False,
                    "challenge": {"detected": False},
                },
            }

    monkeypatch.setattr(session, "tab", lambda *args, **kwargs: Page())
    monkeypatch.setattr(
        "harness.session.parallel_ops.parallel",
        lambda _session, items, fn, **kwargs: [
            {"item": item, "ok": True, "value": fn(item)} for item in items
        ],
    )

    rows = session.namespace()["open_pages"](
        ["https://a.test", "https://b.test", "https://c.test"], total_chars=12_000
    )

    assert len(rows) == 3
    assert {kwargs["max_chars"] for _, kwargs in calls} == {4_000}


def test_parallel_records_remain_composable_until_the_stdout_boundary(session, monkeypatch):
    records = [
        {"item": index, "ok": True, "value": {"payload": "x" * 1_000}}
        for index in range(200)
    ]
    monkeypatch.setattr(
        "harness.session.parallel_ops.parallel",
        lambda _session, items, fn, **kwargs: records,
    )
    namespace = session.namespace()

    returned = namespace["parallel"]([], lambda item: item)
    summary = namespace["summarise"](returned)

    assert returned is records
    assert summary["total"] == 200 and summary["ok"] == 200






























def test_helpers_keep_their_names_so_a_traceback_is_readable(session):
    ns = session.namespace()
    assert ns["goto"].__name__ == "goto" and ns["fill_form"].__name__ == "fill_form"


# --- run_script ---------------------------------------------------------------

def test_a_script_runs_against_the_session(served, capsys):
    assert run_script('print(js("hi"))', name="sesstest") == 0
    assert capsys.readouterr().out.strip() == "a"


def test_a_multi_megabyte_stdout_value_is_spilled_and_fetchable(
        served, capsys, monkeypatch, tmp_path):

    from harness.core.content import ContentStore

    monkeypatch.setenv("BH_OUTPUT_BYTES", "100")
    monkeypatch.setenv("BH_CONTENT_STORE", str(tmp_path / "content"))
    assert run_script('print("x" * 1_000_000)', name="sesstest") == 0
    marker = json.loads(capsys.readouterr().out)

    assert marker["surface"] == "stdout" and marker["_elided"] == 1_000_001
    assert ContentStore(tmp_path / "content").get(marker["_sha256"]) == "x" * 1_000_000 + "\n"


def test_a_large_raw_helper_result_is_reversibly_elided_before_the_agent_sees_it(
        served, monkeypatch, tmp_path):
    from harness.core.content import ContentStore

    browser, _ = served
    browser.eval_hook = lambda expression: "a" * 1_000
    monkeypatch.setenv("BH_OUTPUT_BYTES", "100")
    store = ContentStore(tmp_path / "content")
    session = Session("sesstest", content_store=store)
    try:
        marker = session.namespace()["js"]("x")
        assert marker["surface"] == "js"
        assert store.get(marker["_sha256"]) == "a" * 1_000
    finally:
        session.close()


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


# -- stdio encoding (upstream #359) ------------------------------------------

def test_force_utf8_streams_pins_all_three_streams(monkeypatch):
    """On Windows, stdout defaults to the ANSI code page, so `print(page_text())` — shown on
    nearly every SKILL.md example — dies with UnicodeEncodeError on any CJK or emoji page."""
    import sys

    from harness.session import force_utf8_streams

    calls = {}

    class Stream:
        def __init__(self, tag):
            self.tag = tag

        def reconfigure(self, **kw):
            calls[self.tag] = kw

    monkeypatch.setattr(sys, "stdin", Stream("stdin"))
    monkeypatch.setattr(sys, "stdout", Stream("stdout"))
    monkeypatch.setattr(sys, "stderr", Stream("stderr"))

    force_utf8_streams()

    assert set(calls) == {"stdin", "stdout", "stderr"}
    # stdin strips a BOM (PowerShell redirects write one); writes must never emit one.
    assert calls["stdin"] == {"encoding": "utf-8-sig", "errors": "replace"}
    assert calls["stdout"] == {"encoding": "utf-8", "errors": "replace"}
    assert calls["stderr"] == {"encoding": "utf-8", "errors": "replace"}


def test_force_utf8_streams_survives_streams_that_cannot_reconfigure(monkeypatch):
    """pytest's capture, a plain StringIO, and an already-read stdin all turn up here. A
    convenience that crashes the run it was meant to protect is worse than no convenience."""
    import io
    import sys

    from harness.session import force_utf8_streams

    class Refuses:
        def reconfigure(self, **kw):
            raise ValueError("underlying buffer has been detached")

    monkeypatch.setattr(sys, "stdin", io.StringIO())          # no reconfigure attribute
    monkeypatch.setattr(sys, "stdout", Refuses())             # raises
    monkeypatch.setattr(sys, "stderr", object())              # not a stream at all

    force_utf8_streams()                                       # must not raise


def test_a_non_ascii_page_dump_does_not_kill_the_script(monkeypatch):
    """The end the fix exists for: replacement beats losing the run. The harness promised
    the page's *text*, never its exact bytes."""
    import sys

    from harness.session import force_utf8_streams

    class AnsiStdout:
        """Stands in for a cp1252 stdout: refuses non-latin-1 until it is reconfigured."""

        def __init__(self):
            self.encoding = "cp1252"
            self.buf = []

        def write(self, s):
            s.encode(self.encoding, "strict")     # raises UnicodeEncodeError under cp1252
            self.buf.append(s)
            return len(s)

        def flush(self):
            pass

        def reconfigure(self, *, encoding=None, errors=None):
            self.encoding = encoding or self.encoding

    stream = AnsiStdout()
    monkeypatch.setattr(sys, "stdout", stream)

    with pytest.raises(UnicodeEncodeError):
        print("\u6c42\u4eba \u5fdc\u52df")

    force_utf8_streams()
    print("\u6c42\u4eba \u5fdc\u52df")
    assert "\u6c42\u4eba" in "".join(stream.buf)
