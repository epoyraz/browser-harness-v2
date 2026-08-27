"""The application-state wait, tested where it now lives.

Moved from `test_page.py`. The classification — form, account wall, bot wall, usable UI —
is domain judgment and sits in `applications/`; the observer it runs on,
`Tab.watch_document`, stayed in the harness because "run one observer across document
replacements" is not about job applications.
"""
import json
import time

import pytest

from applications.state import wait_for_application_state
from tests.unit.conftest import _tab


def test_application_state_returns_a_real_form_immediately(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: (
        {"matched": True, "immediate": True, "state": "form", "fields": 8,
         "controls": 11, "text_len": 844, "title": "Software Architect",
         "url": "https://jobs.test/application", "ready_state": "complete"}
        if "hasSubmit" in e else None)
    result = wait_for_application_state(tab, timeout=2.0)
    assert result["state"] == "form" and result["immediate"] is True
    assert result["waited_ms"] < 500


def test_application_state_requires_a_quiet_usable_ui(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: (
        {"matched": False, "immediate": False, "state": "usable_ui", "fields": 0,
         "controls": 1, "text_len": 900, "title": "Job", "url": "https://a.test/",
         "ready_state": "complete"}
        if "hasSubmit" in e else None)
    started = time.monotonic()
    result = wait_for_application_state(tab, 
        timeout=1.0, usable_stable=0.08, empty_stable=0.2)
    assert result["state"] == "usable_ui" and result["reason"] == "stable"
    assert time.monotonic() - started >= 0.06


def test_application_state_does_not_treat_title_plus_empty_body_as_terminal(wired):
    browser, _, _ = wired
    tab = _tab(wired)
    browser.eval_hook = lambda e: (
        {"matched": False, "immediate": False, "state": "loading", "fields": 0,
         "controls": 0, "text_len": 0, "title": "Backend Engineer @ Air Apps",
         "url": "https://jobs.test/posting", "ready_state": "complete"}
        if "hasSubmit" in e else None)
    started = time.monotonic()
    result = wait_for_application_state(tab, 
        timeout=1.0, usable_stable=0.05, empty_stable=0.12)
    assert result["state"] == "stable_failure" and result["immediate"] is False
    assert time.monotonic() - started >= 0.10


def test_application_state_rejects_invalid_stability_windows(wired):
    with pytest.raises(ValueError):
        wait_for_application_state(_tab(wired), empty_stable=0)


def test_a_terminal_application_state_owes_the_page_no_teardown(wired):
    """The watch script disconnects and forgets its observer before reporting a match, so
    the teardown evaluation that used to run unconditionally asked the page to delete
    something that was already gone. Measured on the 2026-08-25 corpus: one wasted
    `Runtime.evaluate` per call, 174 of `wait_for_application_state`'s 516."""
    browser, _, _ = wired
    tab = _tab(wired)
    tab._ensure_world()                        # pay the world once, outside the measurement
    browser.eval_hook = lambda e: (
        {"matched": True, "immediate": True, "state": "form", "fields": 8,
         "controls": 11, "text_len": 844, "title": "Job",
         "url": "https://jobs.test/apply", "ready_state": "complete"}
        if "hasSubmit" in e else None)

    mark = len(browser.calls)
    assert wait_for_application_state(tab, timeout=2.0)["state"] == "form"
    assert [c.get("method") for c in browser.calls[mark:]] == ["Runtime.evaluate"]


def test_an_abandoned_observer_is_still_torn_down(wired):
    """The other half: a wait that gives up left an observer running, and a MutationObserver
    on a busy page runs its callback for the life of the document. That one must still cost
    a round trip."""
    browser, _, _ = wired
    tab = _tab(wired)
    tab._ensure_world()
    browser.eval_hook = lambda e: (
        {"matched": False, "immediate": False, "state": "loading", "fields": 0,
         "controls": 0, "text_len": 0, "title": "Job", "url": "https://a.test/",
         "ready_state": "complete"}
        if "hasSubmit" in e else None)

    mark = len(browser.calls)
    result = wait_for_application_state(tab, 
        timeout=0.5, usable_stable=0.05, empty_stable=0.05)
    assert result["state"] == "stable_failure"
    evaluated = [c for c in browser.calls[mark:] if c.get("method") == "Runtime.evaluate"]
    assert "__bh.watch" in json.dumps(evaluated[-1]["params"])

