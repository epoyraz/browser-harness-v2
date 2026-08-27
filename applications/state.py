"""Is this page an application, an account wall, a bot wall, or still loading?

That question is domain judgment, so it lives here. What it is built on is not: the
harness exposes `Tab.watch_document`, which runs one observer across document
replacements and wakes on DOM mutation rather than polling. The expression it watches is
the domain part — what counts as a form, what counts as a wall — and it is this module's.
"""
from __future__ import annotations

import json
import time
from typing import Any

from harness.ops.page import Tab

WATCH_APPLICATION_STATE_JS = """((token) => {
  const bh = window.__bh;
  // Before the read, not after: `watch_document` re-evaluates this on every wakeup, and
  // a run that reports `matched` returns below without reaching the arming code. Clearing
  // here means a match leaves nothing behind whichever branch it takes, so the caller
  // never owes the page a teardown round trip.
  bh.watch = bh.watch || {};
  if (bh.watch[token]) { bh.watch[token].disconnect(); delete bh.watch[token]; }
  let fields = 0;
  for (const el of document.querySelectorAll(
       'input,select,textarea,[contenteditable=true],[role=combobox]')) {
    const type = (el.type || '').toLowerCase();
    if (['submit', 'button', 'reset', 'image', 'hidden', 'search'].includes(type)) continue;
    if (bh.furniture(el) || !bh.visible(el)) continue;
    fields++;
  }
  const controls = [...document.querySelectorAll(
    'button,a[href],[role=button],input,select,textarea')].filter(bh.visible);
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
  const password = [...document.querySelectorAll('input[type=password]')].some(bh.visible);
  const applicationFiles = document.querySelectorAll('input[type=file]').length;
  const structural = location.href + ' ' + title + ' ' + labels + ' ' +
    [...document.querySelectorAll('input,select,textarea')].map(el =>
      [el.id, el.name, el.getAttribute('aria-label')].filter(Boolean).join(' ')).join(' ');
  const applicationStructure = applicationFiles > 0 ||
    (fields >= 3 && /(apply|application|bewerb|postul|candidat|candidature)/i.test(structural));
  const accountWall = !applicationStructure && (password || (
    /(sign in|log in|login|anmelden|connexion|create an account|konto erstellen)/i.test(lower)
    && fields < 2 && controls.length > 0));

  let state = 'loading';
  if (botWall) state = 'bot_wall';
  else if (accountWall) state = 'account_wall';
  else if (fields >= 2 && (hasSubmit || document.querySelector('form'))) state = 'form';
  else if (text.length >= 40 || controls.length > 0 || hasApply) state = 'usable_ui';
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
    if timeout <= 0 or usable_stable <= 0 or empty_stable <= 0:
        raise ValueError("timeout and stability windows must be positive")
    token = f"a{id(tab)}:{time.perf_counter_ns()}"
    started = time.perf_counter()
    with tab.journal.call("wait_for_application_state",
                      usable_stable=usable_stable, empty_stable=empty_stable):
        probe, reason = tab.watch_document(
            token,
            WATCH_APPLICATION_STATE_JS.replace("__TOKEN__", json.dumps(token)),
            timeout=timeout,
            stable_for=lambda value: usable_stable
            if value.get("state") == "usable_ui" else empty_stable,
        )
    final_state = (str(probe.get("state")) if probe.get("matched")
                   else "usable_ui" if probe.get("state") == "usable_ui"
                   else "stable_failure")

    return {**probe, "state": final_state, "reason": reason,
            "immediate": bool(probe.get("immediate")) if reason == "terminal" else False,
            "waited_ms": round((time.perf_counter() - started) * 1000, 1)}

