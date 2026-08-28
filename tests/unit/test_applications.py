"""The application workflow, tested where it now lives.

Moved from `test_session.py` unchanged in substance. The workflow is a set of functions
over a session rather than methods on it, so `s.run_application(...)` reads
`run_application(s, ...)`; what these assert — the transition shapes, the skill packet's
digest verification, the planner signatures, and that no path can submit — is the same.
"""
import hashlib
import json

from applications import (
    application_skills,
    follow_application,
    locate_application,
    prepare_application,
    run_application,
    state,
    workflow,
)


def _application_skill_source(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    body_path = root / "apply-test" / "SKILL.md"
    body_path.parent.mkdir(parents=True)
    body = "---\nname: apply-test\ndescription: Test public applications.\n---\n\n# Test\n"
    body_bytes = body.encode("utf-8")
    body_path.write_bytes(body_bytes)
    digest = "sha256:" + hashlib.sha256(body_bytes).hexdigest()
    (root / "index.json").write_text(json.dumps({"schema": 1, "skills": [{
        "id": "apply/test", "version": "1.0.0", "description": "Test",
        "path": "apply-test/SKILL.md", "match": [{"host": "*.test"}],
        "digest": digest,
    }]}), encoding="utf-8")
    config = tmp_path / "sources.toml"
    config.write_text(f'''[[source]]
name = "test-public"
type = "path"
trust = "public"
priority = 10
path = {json.dumps(str(root))}
''', encoding="utf-8")
    monkeypatch.setenv("BH_SKILLS_SOURCES", str(config))


def test_prepare_application_stops_before_frame_discovery_when_main_is_a_form(session, served):
    browser, _ = served
    payload = {"schema": {"verdict": {"is_form": True}, "fields": [{"ref": "e1"}]},
               "url": "https://a.test/apply", "title": "Apply", "language": "en",
               "file_inputs": [], "apply_link": None}
    browser.eval_hook = lambda expression: (
        True if "__bhDryRunGuardInstalled" in expression else payload)
    prepared = prepare_application(session)
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
    prepared = prepare_application(session)
    assert prepared["contexts_checked"] == 1 and prepared["is_application"] is True
    assert not any(call.get("method") == "Target.setAutoAttach" for call in browser.calls)


def test_prepare_application_prefers_explicit_application_verdict(session, served):
    browser, _ = served
    payload = {"schema": {"verdict": {"is_form": True, "is_application": False,
                                        "classification": "generic_form"},
                          "fields": [{"ref": f"e{i}"} for i in range(8)]},
               "url": "https://a.test/contact", "title": "Contact", "language": "en",
               "file_inputs": [], "apply_link": None}
    browser.eval_hook = lambda expression: (
        True if "__bhDryRunGuardInstalled" in expression else payload)
    prepared = prepare_application(session)
    assert prepared["is_application"] is False


def test_prepare_application_accepts_explicit_account_bearing_application(session, served):
    browser, _ = served
    payload = {"schema": {"verdict": {"is_form": True, "is_application": True,
                                        "classification": "application_form_with_account_fields"},
                          "fields": [{"ref": "email"}, {"ref": "password"}]},
               "url": "https://a.test/Application/New/1", "title": "Apply", "language": "en",
               "file_inputs": [{"ref": "cv"}], "apply_link": None}
    browser.eval_hook = lambda expression: (
        True if "__bhDryRunGuardInstalled" in expression else payload)
    prepared = prepare_application(session)
    assert prepared["is_application"] is True and prepared["contexts_checked"] == 1


def test_follow_application_switches_to_a_new_target(session, served, monkeypatch):
    browser, _ = served
    origin = session.use_tab("a")
    browser.targets["popup"] = {
        "targetId": "popup", "type": "page", "url": "https://a.test/application"}
    _popup = session.tab("popup")
    session.use_tab("a")
    monkeypatch.setattr(origin, "click_ref", lambda *a, **kw: {
        "url_before": "https://a.test/job", "url_after": "https://a.test/job",
        "navigated": False, "dom_mutations": 0, "new_targets": ["popup"],
        "dialog": None})
    monkeypatch.setattr(state, "wait_for_application_state", lambda *a, **kw: {
        "state": "form", "fields": 8})

    result = follow_application(session, {
        "url": "https://a.test/job", "apply_control": {"ref": "e1"},
        "apply_link": "https://a.test/application"})
    assert result["transition"]["kind"] == "new_target"
    assert result["target_changed"] is True and session.tab().target_id == "popup"


def test_follow_application_uses_discovered_link_after_an_inert_click(
        session, monkeypatch):
    origin = session.use_tab("a")
    monkeypatch.setattr(origin, "click_ref", lambda *a, **kw: {
        "url_before": "https://a.test/job", "url_after": "https://a.test/job",
        "navigated": False, "dom_mutations": 0, "new_targets": [], "dialog": None})
    monkeypatch.setattr(origin, "goto", lambda url, **kw: {
        "requested": url, "landed": url, "lifecycle": "load"})
    monkeypatch.setattr(state, "wait_for_application_state", lambda *a, **kw: {
        "state": "form", "fields": 8})

    result = follow_application(session, {
        "url": "https://a.test/job", "apply_control": {"ref": "e1"},
        "apply_link": "https://a.test/job/application"})
    assert result["transition"]["kind"] == "fallback_link"
    assert result["state"]["state"] == "form"


def test_follow_application_uses_an_ats_route_candidate(session, monkeypatch):
    origin = session.use_tab("a")
    visited = []
    monkeypatch.setattr(origin, "goto", lambda url, **kw: (
        visited.append(url) or {"requested": url, "landed": url, "lifecycle": "load"}))
    monkeypatch.setattr(state, "wait_for_application_state", lambda *a, **kw: {
        "state": "form", "fields": 8})
    candidate = "https://jobs.test/acme/id/application"
    result = follow_application(session, 
        {"url": "https://jobs.test/acme/id", "apply_control": None,
         "apply_link": None}, candidates=[candidate])
    assert visited == [candidate]
    assert result["transition"]["kind"] == "candidate_link"


def test_locate_application_reuses_transition_state_and_reconciles_form(
        session, monkeypatch):
    tab = session.use_tab("a")
    waits = []
    monkeypatch.setattr(tab, "goto", lambda url, **kw: {
        "requested": url, "landed": url, "lifecycle": "load"})
    monkeypatch.setattr(state, "wait_for_application_state", lambda *a, **kw: (
        waits.append("wait") or {"state": "usable_ui"}))
    prepared = iter([
        {"url": "https://a.test/job", "target_id": "a", "is_application": False,
         "schema": {"verdict": {"is_form": False, "fields": 0}},
         "apply_control": {"ref": "e1"}, "apply_link": None,
         "context": "main", "contexts_checked": 1},
        {"url": "https://a.test/apply", "target_id": "a", "is_application": True,
         "schema": {"verdict": {"is_form": True, "fields": 8}},
         "apply_control": None, "apply_link": None,
         "context": "main", "contexts_checked": 1},
    ])
    monkeypatch.setattr(workflow, "prepare_application",
                        lambda _session, **kw: next(prepared))
    monkeypatch.setattr(workflow, "follow_application", lambda *a, **kw: {
        "transition": {"kind": "control"}, "state": {"state": "account_wall"},
        "target_id": "a", "target_changed": False})

    result = locate_application(session, "https://a.test/job")
    assert waits == ["wait"]  # the second hop reuses follow_application's observation
    assert result["terminal_state"] == "form"
    assert result["hops"][-1]["reconciled_state"] == "form"
    assert result["hops"][-1]["state_conflict"] is True


# §2.3: a posting whose apply view the route table can name. The rule is Ashby's
# `/<company>/<uuid>` -> `/application`, so these two URLs are one `goto` apart.
_ROUTED_POSTING = "https://jobs.ashbyhq.com/acme/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_ROUTED_FORM = _ROUTED_POSTING + "/application"


def _locate_double(session, monkeypatch, states, prepared, *, follow=None):
    """Drive `locate_application` over a scripted browser: returns the URLs visited."""
    tab = session.use_tab("a")
    visited = []
    monkeypatch.setattr(tab, "goto", lambda url, **kw: (
        visited.append(url) or {"requested": url, "landed": url, "lifecycle": "load"}))
    scripted = iter(states)
    monkeypatch.setattr(state, "wait_for_application_state",
                        lambda *a, **kw: next(scripted))
    documents = iter(prepared)
    monkeypatch.setattr(workflow, "prepare_application",
                        lambda _session, **kw: next(documents))
    monkeypatch.setattr(workflow, "follow_application", follow or (lambda *a, **kw: {
        "transition": {"kind": "control"}, "state": {"state": "form"},
        "target_id": "a", "target_changed": False}))
    return visited


def _document(url, *, is_application, control=None):
    return {"url": url, "target_id": "a", "is_application": is_application,
            "schema": {"verdict": {"is_form": is_application,
                                   "fields": 8 if is_application else 0}},
            "apply_control": control, "apply_link": None,
            "context": "main", "contexts_checked": 1}


def test_locate_application_starts_at_the_route_candidate_when_it_is_a_form(
        session, monkeypatch):
    monkeypatch.delenv("BH_APPLICATION_ROUTE_FIRST", raising=False)
    visited = _locate_double(
        session, monkeypatch,
        states=[{"state": "form", "fields": 8}],
        prepared=[_document(_ROUTED_FORM, is_application=True)])

    result = locate_application(session, _ROUTED_POSTING)

    assert visited == [_ROUTED_FORM]  # the posting is never navigated to
    assert result["hops"][0]["via"] == "route_rule"
    assert result["hops"][0]["accepted"] is True
    assert result["hops"][0]["start_url"] == _ROUTED_POSTING
    assert result["hops"][1]["hop"] == 1 and result["hops"][1]["url"] == _ROUTED_FORM
    assert result["terminal_state"] == "form"


def test_locate_application_falls_back_to_the_posting_when_the_candidate_is_not_a_form(
        session, monkeypatch):
    monkeypatch.delenv("BH_APPLICATION_ROUTE_FIRST", raising=False)
    followed = []
    visited = _locate_double(
        session, monkeypatch,
        states=[{"state": "stable_failure"}, {"state": "usable_ui"}],
        prepared=[_document(_ROUTED_POSTING, is_application=False, control={"ref": "e1"}),
                  _document(_ROUTED_FORM, is_application=True)],
        follow=lambda *a, **kw: (followed.append(kw.get("candidates")) or {
            "transition": {"kind": "control"}, "state": {"state": "form"},
            "target_id": "a", "target_changed": False}))

    result = locate_application(session, _ROUTED_POSTING)

    # The original path runs from the posting, and the fallback costs one extra `goto`
    # plus the candidate's own state wait: the two scripted states are consumed one by the
    # route probe and one by the posting's hop, so the posting inherits nothing from the
    # rejected candidate. Without route-first this case needs only the second of them.
    assert visited == [_ROUTED_FORM, _ROUTED_POSTING]
    assert result["hops"][0]["via"] == "route_rule"
    assert result["hops"][0]["accepted"] is False
    assert result["hops"][0]["application_state"] == {"state": "stable_failure"}
    assert result["hops"][1]["url"] == _ROUTED_POSTING
    assert result["hops"][1]["application_state"] == {"state": "usable_ui"}
    assert followed == [[_ROUTED_FORM]]  # the candidate stays available to the old path
    assert result["terminal_state"] == "form"


def test_locate_application_is_unchanged_when_no_route_rule_matches(session, monkeypatch):
    monkeypatch.delenv("BH_APPLICATION_ROUTE_FIRST", raising=False)
    visited = _locate_double(
        session, monkeypatch,
        states=[{"state": "form", "fields": 8}],
        prepared=[_document("https://a.test/apply", is_application=True)])

    result = locate_application(session, "https://a.test/apply")

    assert visited == ["https://a.test/apply"]
    assert [row["hop"] for row in result["hops"]] == [0]
    assert "via" not in result["hops"][0]


def test_locate_application_route_first_toggle_restores_landing_on_the_posting(
        session, monkeypatch):
    monkeypatch.setenv("BH_APPLICATION_ROUTE_FIRST", "0")
    visited = _locate_double(
        session, monkeypatch,
        states=[{"state": "usable_ui"}],
        prepared=[_document(_ROUTED_POSTING, is_application=True)])

    result = locate_application(session, _ROUTED_POSTING)

    assert visited == [_ROUTED_POSTING]
    assert [row["hop"] for row in result["hops"]] == [0]
    assert "via" not in result["hops"][0]


def test_run_application_plans_and_fills_without_a_submit_operation(session, monkeypatch):
    monkeypatch.setattr(workflow, "locate_application", lambda *a, **kw: {
        "terminal_state": "form", "prepared": {
            "is_application": True, "schema": {"fields": [{"ref": "e1"}]},
            "language": "en"}, "hops": [], "navigation": {}, "wall_ms": 1})
    monkeypatch.setattr("harness.session.forms.fill_form", lambda tab, plan, **kw: type(
        "Filled", (), {"ok": True, "to_json": lambda self: {"ok": True}})())
    result = run_application(session, 
        "https://a.test", planner=lambda schema, language: (
            [{"ref": "e1", "value": "Enes"}], [{"status": "planned"}]),
        skills=False)
    assert result["stage"] == "filled" and result["fill"] == {"ok": True}
    assert result["audit"] == [{"status": "planned"}]
    assert "prepared" not in result  # location is the single authoritative copy


def test_run_application_forwards_human_readable_presentation(session, monkeypatch):
    monkeypatch.setattr(workflow, "locate_application", lambda *a, **kw: {
        "terminal_state": "form", "prepared": {
            "is_application": True, "schema": {"fields": [{"ref": "e1"}]},
            "language": "en"}, "hops": [], "navigation": {}, "wall_ms": 1})
    seen = {}

    def filled(tab, plan, **kwargs):
        seen.update(kwargs)
        return type("Filled", (), {"ok": True,
                                    "to_json": lambda self: {"ok": True}})()

    monkeypatch.setattr("harness.session.forms.fill_form", filled)
    run_application(session, 
        "https://a.test", planner=lambda schema, language: [{"ref": "e1", "value": "x"}],
        skills=False, human_readable=True, human_pause=0.4)

    assert seen["human_readable"] is True and seen["human_pause"] == 0.4


def test_application_skills_are_offline_digest_verified_and_publicly_delimited(
        session, served, tmp_path, monkeypatch):
    _application_skill_source(tmp_path, monkeypatch)
    browser, _daemon = served
    before = len(browser.calls)

    packet = application_skills(session, "https://jobs.test/role")

    assert len(browser.calls) == before
    assert packet["matches"][0]["id"] == "apply/test"
    assert "path" not in packet["matches"][0]
    assert packet["model_context"].startswith(
        '<untrusted-skill-reference source="test-public" id="apply/test">')
    assert packet["bytes"] == len(packet["model_context"].encode())
    assert packet["sha256"] == hashlib.sha256(packet["model_context"].encode()).hexdigest()


def test_run_application_injects_skills_only_into_a_compatible_planner(
        session, tmp_path, monkeypatch):
    _application_skill_source(tmp_path, monkeypatch)
    monkeypatch.setattr(workflow, "locate_application", lambda *a, **kw: {
        "terminal_state": "form", "prepared": {
            "is_application": True, "url": "https://jobs.test/role/apply",
            "schema": {"fields": [{"ref": "e1"}]}, "language": "en"},
        "hops": [{"url": "https://jobs.test/role"}],
        "navigation": {}, "wall_ms": 1})
    monkeypatch.setattr("harness.session.forms.fill_form", lambda tab, plan, **kw: type(
        "Filled", (), {"ok": True, "to_json": lambda self: {"ok": True}})())
    received = {}

    def planner(schema, language, skill_context):
        received.update(skill_context)
        return [{"ref": "e1", "value": "x"}]

    result = run_application(session, "https://jobs.test/role", planner=planner)

    assert received["matches"][0]["id"] == "apply/test"
    assert received == result["skills"]
    assert result["stage"] == "filled"


def test_run_application_keeps_two_argument_planners_compatible(
        session, tmp_path, monkeypatch):
    _application_skill_source(tmp_path, monkeypatch)
    monkeypatch.setattr(workflow, "locate_application", lambda *a, **kw: {
        "terminal_state": "form", "prepared": {
            "is_application": True, "url": "https://jobs.test/role/apply",
            "schema": {"fields": [{"ref": "e1"}]}, "language": "en"},
        "hops": [], "navigation": {}, "wall_ms": 1})
    monkeypatch.setattr("harness.session.forms.fill_form", lambda tab, plan, **kw: type(
        "Filled", (), {"ok": True, "to_json": lambda self: {"ok": True}})())
    calls = []

    result = run_application(session, 
        "https://jobs.test/role",
        planner=lambda schema, language: calls.append((schema, language)) or [])

    assert len(calls) == 1
    assert result["skills"]["matches"][0]["id"] == "apply/test"

