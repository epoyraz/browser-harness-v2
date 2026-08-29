"""Is this page an application, an account wall, a bot wall, or still loading?

That question is domain judgment, so it lives here. What it is built on is not: the
harness exposes `Tab.watch_document`, which runs one observer across document
replacements and wakes on DOM mutation rather than polling. The expression it watches is
the domain part — what counts as a form, what counts as a wall — and it is this module's.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from harness.ops.page import Tab


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default

WATCH_APPLICATION_STATE_JS = """((token) => {
  const bh = window.__bh;
  // Before the read, not after: `watch_document` re-evaluates this on every wakeup, and
  // a run that reports `matched` returns below without reaching the arming code. Clearing
  // here means a match leaves nothing behind whichever branch it takes, so the caller
  // never owes the page a teardown round trip.
  bh.watch = bh.watch || {};
  if (bh.watch[token]) { bh.watch[token].disconnect(); delete bh.watch[token]; }
  // Shadow-DOM aware when the runtime offers it (SmartRecruiters mounts its whole form
  // under shadow hosts); the plain query is the fallback for an older daemon.
  const q = bh.deepAll ? (s => bh.deepAll(s)) : (s => [...document.querySelectorAll(s)]);
  let fields = 0;
  for (const el of q(
       'input,select,textarea,[contenteditable=true],[role=combobox]')) {
    const type = (el.type || '').toLowerCase();
    if (['submit', 'button', 'reset', 'image', 'hidden', 'search'].includes(type)) continue;
    if (bh.furniture(el) || !bh.visible(el)) continue;
    fields++;
  }
  const controls = q('button,a[href],[role=button],input,select,textarea').filter(bh.visible);
  const labels = controls.map(el =>
    (el.innerText || el.value || el.getAttribute('aria-label') || '').trim()).join(' ');
  const text = ((document.body && document.body.innerText) || '').trim();
  const lower = (text + ' ' + labels).toLowerCase();
  const title = (document.title || '').trim();
  const hasSubmit = controls.some(el => {
    const label = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
    return /submit application|send application|bewerbung senden|postuler|candidature/i.test(label)
      || (el.tagName === 'INPUT' && (el.type || '').toLowerCase() === 'submit');
  });
  const hasApply = controls.some(el => {
    const label = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
    return /(apply|bewerb|postul|candidat|sollicit|aplicar)/i.test(label);
  });
  const botWall = /(captcha|verify you are human|checking your browser|access denied|unusual traffic|robot check|security challenge)/i.test(lower);
  const password = q('input[type=password]').some(bh.visible);
  const applicationFiles = q('input[type=file]').length;
  const structural = location.href + ' ' + title + ' ' + labels + ' ' +
    q('input,select,textarea').map(el =>
      [el.id, el.name, el.getAttribute('aria-label')].filter(Boolean).join(' ')).join(' ');
  const applicationStructure = applicationFiles > 0 ||
    (fields >= 3 && /(apply|application|bewerb|postul|candidat|candidature)/i.test(structural));
  const accountWall = !applicationStructure && (password || (
    /(sign in|log in|login|anmelden|connexion|create an account|konto erstellen)/i.test(lower)
    && fields < 2 && controls.length > 0));

  // A complete document that painted nothing while its tab is hidden will not render
  // until the tab is activated (Abacus Umantis jobportal, pastaHR, Workday apply —
  // measured 2026-08-29): waiting `empty_stable` for it buys nothing. Terminal, so the
  // caller can activate or move on. `BH_APPLICATION_HIDDEN_BLANK=0` disables the state.
  // Headless Chrome reports background tabs as hidden too, but paints them regardless
  // (measured 2026-08-29: the Abacus jobportal rendered in headless and this verdict
  // cost a form there), so the state is for headed browsers only.
  const headless = /HeadlessChrome/i.test(navigator.userAgent || '');
  const hiddenBlank = '__HIDDEN_BLANK__' !== '0' && !headless && document.visibilityState === 'hidden'
    && document.readyState === 'complete' && text.length === 0 && controls.length === 0
    && fields === 0;
  let state = 'loading';
  if (botWall) state = 'bot_wall';
  else if (accountWall) state = 'account_wall';
  else if (fields >= 2 && (hasSubmit || document.querySelector('form'))) state = 'form';
  else if (text.length >= 40 || controls.length > 0 || hasApply) state = 'usable_ui';
  else if (hiddenBlank) state = 'hidden_blank';
  const result = {state, fields, controls: controls.length, text_len: text.length,
                  application_structure: applicationStructure,
                  title, url: location.href, ready_state: document.readyState,
                  matched: ['form', 'account_wall', 'bot_wall'].includes(state)};
  if (result.matched) return {...result, immediate: true};

  const obs = new MutationObserver(() => {
    obs.disconnect();
    delete bh.watch[token];
    __bhNotify(token);
  });
  obs.observe(document.documentElement || document,
    {subtree: true, childList: true, attributes: true, characterData: true});
  bh.watch[token] = obs;
  return {...result, immediate: false};
})(__TOKEN__)"""


def wait_for_application_state(tab: Tab, *, timeout: float = 12.0,
                               usable_stable: float = 0.8,
                               empty_stable: float = 5.0) -> dict[str, Any]:
    """Wait for a form, usable UI, account wall, bot wall, or stable failure.

    `load` is not a UI readiness signal for client-rendered ATS pages.  In particular,
    Ashby can have a correct title while `<body>` is still empty.  Strong states return
    immediately; ordinary content must remain mutation-free for `usable_stable`, and
    an empty document must remain quiet for the longer `empty_stable` before it is
    called a failure.  DOM mutations and document replacements wake this wait rather
    than a polling loop.
    """
    # Experiment knobs (2026-08-29): the stability windows and the hidden-blank state are
    # overridable from the environment so two arms can run the same binary.
    usable_stable = _env_float("BH_APPLICATION_USABLE_STABLE", usable_stable)
    empty_stable = _env_float("BH_APPLICATION_EMPTY_STABLE", empty_stable)
    hidden_stable = _env_float("BH_APPLICATION_HIDDEN_STABLE", 1.5)
    hidden_blank = os.environ.get("BH_APPLICATION_HIDDEN_BLANK", "1").strip() != "0"
    if timeout <= 0 or usable_stable <= 0 or empty_stable <= 0:
        raise ValueError("timeout and stability windows must be positive")
    token = f"a{id(tab)}:{time.perf_counter_ns()}"
    started = time.perf_counter()

    def stable_for(value: dict[str, Any]) -> float:
        state = value.get("state")
        if state == "usable_ui":
            return usable_stable
        if state == "hidden_blank":
            # Ashby renders into an empty body a moment after `complete` even in a hidden
            # tab, so the blank verdict still needs a short quiet window — just not the
            # full `empty_stable` that a page which will never paint pays for nothing.
            return hidden_stable
        return empty_stable

    with tab.journal.call("wait_for_application_state",
                      usable_stable=usable_stable, empty_stable=empty_stable):
        probe, reason = tab.watch_document(
            token,
            WATCH_APPLICATION_STATE_JS.replace("__TOKEN__", json.dumps(token))
                                      .replace("'__HIDDEN_BLANK__'", "'1'" if hidden_blank else "'0'"),
            timeout=timeout,
            stable_for=stable_for,
        )
    final_state = (str(probe.get("state")) if probe.get("matched")
                   else "usable_ui" if probe.get("state") == "usable_ui"
                   else "hidden_blank" if probe.get("state") == "hidden_blank"
                   else "stable_failure")

    return {**probe, "state": final_state, "reason": reason,
            "immediate": bool(probe.get("immediate")) if reason == "terminal" else False,
            "waited_ms": round((time.perf_counter() - started) * 1000, 1)}

