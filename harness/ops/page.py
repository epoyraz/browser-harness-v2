"""Agent-facing page primitives (DESIGN.md D3/D4/D11/D13/D14, TODO 15–21).

One `Tab` per target, built on the Phase 1 registry — a Tab never makes its own session.
The contract carried through every method:

  - Timeouts are **arguments, never env vars** (§6). v1's one global `BH_IPC_TIMEOUT`
    had to cover both a 5 ms box-model read and a 90 s in-page fetch.
  - Failures are typed with their evidence (D11): `goto()` returns `requested` AND
    `landed`, `js()` raises `JsException` with the description instead of returning None,
    a click returns a **delta** instead of silence.
  - Waits are event-driven (D13): `Page.lifecycleEvent` wakes a condition variable;
    nothing polls on a 300 ms loop.

The dialog dance, learned the hard way: `Input.dispatchMouseEvent` does not ACK while a
JS dialog opened by the click handler is up — the renderer is blocked inside our own
dispatch. So a click watches for `javascriptDialogOpening`, and a dispatch that times out
with a dialog pending is a *successful click that opened a dialog*, not a failure. The
dialog is auto-dismissed (accept=False by default) and reported in the delta.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from harness.connect.cdp import Connection
from harness.connect.session import SessionRegistry
from harness.core.journal import Journal
from harness.core.outcome import (
    Class,
    ElementGone,
    HarnessError,
    JsException,
    MappingOutcome,
    NavigationFailed,
    NotSerializable,
    Outcome,
    SideEffectRefused,
    Timeout,
    ok,
)

#: The harness's machinery runs in a CDP **isolated world**, not on `window`.
#:
#: Measured: a page can read `Object.getOwnPropertyNames(window)` and see a stray
#: `__bh` global, which announces the harness for no benefit. An isolated world shares the
#: DOM but has its own global object, so page script cannot see our registry at all —
#: `Page.addScriptToEvaluateOnNewDocument(worldName=...)` recreates it on every navigation
#: for free. This is D14 ("use more of CDP, not more code"): the alternative was obfuscating
#: a global name, which only raises the cost of finding it.
#:
#: The user's own `js()` deliberately stays in the **main** world — it is the escape hatch,
#: and code that reaches for page globals must land where the page's globals live.
WORLD = "__bh_world"

#: `frames()` collects OOPIF announcements until this long passes with none arriving.
#: They follow `Target.setAutoAttach` by one round trip, so a quiet window this size is
#: many times the gap it has to bridge — while a frameless page now costs one window
#: instead of two 0.6s sleeps.
FRAMES_QUIET = 0.12
#: Ceiling for the settle loop, so a page that keeps spawning iframes still terminates.
FRAMES_MAX_WAIT = 0.8

#: Installed on every new document (item 18). Idempotent; `__bh.mutations` is the DOM
#: delta counter, `__bh.refs` the snapshot ref registry. Lives in the isolated world, so
#: `__bh` is reachable from harness JS and invisible to the page.
RUNTIME_JS = """(() => {
  const bh = window.__bh || (window.__bh = {refs: {}, n: 0, mutations: 0});
  if (bh.runtime) return;
  bh.runtime = true;
  bh.visible = el => {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return !!(r.width && r.height) && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  bh.furniture = el => {
    if (!el || !el.closest) return false;
    if (el.closest('[role=search],nav,header,aside')) return true;
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      // Feature-detection libraries put tokens such as `cookies` on <html>.  Treating a
      // root/layout container as a cookie component discards the entire application.
      if (node.matches('html,body,main,form')) continue;
      const rawClass = typeof node.className === 'string' ? node.className : '';
      const identity = (String(node.id || '') + ' ' + rawClass).toLowerCase();
      // The container may use camelCase (`cookieConsentBanner`) or a dashed token.
      // Root/layout nodes were rejected above, so substring matching is bounded here.
      const cookieMarker = /cookie/.test(identity);
      if (cookieMarker) return true;
      const consentMarker = /(?:^|[\\s_-])(?:consent|gdpr)(?:[\\s_-]|$)/.test(identity);
      if (!consentMarker) continue;
      // `consent` is also a legitimate required application field.  Exclude it only
      // when the bounded component behaves like a cookie/privacy decision UI.
      const text = String(node.innerText || '').replace(/\\s+/g, ' ').slice(0, 1200);
      if (/(?:cookie|gdpr)/i.test(text)
          && /(?:accept|reject|allow|deny|preferences|manage|akzept|ablehn|zustimm)/i.test(text))
        return true;
    }
    return false;
  };
  bh.ref = el => {
    let ref = el.__bhRef;
    if (!ref || bh.refs[ref] !== el) {
      ref = 'e' + (++bh.n); el.__bhRef = ref; bh.refs[ref] = el;
    }
    return ref;
  };
  const obs = new MutationObserver(list => { bh.mutations += list.length; });
  const arm = () => obs.observe(document.documentElement || document,
    {subtree: true, childList: true, attributes: true, characterData: true});
  document.documentElement ? arm() : document.addEventListener('DOMContentLoaded', arm);
  // Delivery counters, same idea as `mutations`: raw Input.* events are delivered to a
  // renderer, and the renderer silently DROPS mouse and key events for any tab that is
  // not its window's selected tab — the CDP call ACKs either way. Measured with
  // page-side listeners: background tab, dispatchKeyEvent x2 -> 0 keydowns seen,
  // dispatchMouseEvent -> 0 clicks; same tab after Target.activateTarget -> all arrive.
  // A capture listener sees every keydown/scroll that actually reached the document, so
  // "did my keystrokes land?" becomes a counter delta instead of a guess. Registered
  // from the isolated world, so page script cannot enumerate or see these.
  bh.keys = bh.keys || 0;
  bh.scrolls = bh.scrolls || 0;
  document.addEventListener('keydown', () => { bh.keys++; }, true);
  document.addEventListener('scroll', () => { bh.scrolls++; },
    {capture: true, passive: true});
})()"""

#: Installed in the page's main world before page script. This is deliberately separate
#: from the isolated-world runtime: form handlers and network APIs live in the main world,
#: and a guard in another JavaScript realm cannot interpose on them. The closure retains
#: the native functions before application code can capture them. Filling and inspecting
#: remain available; irreversible requests are default-denied and counted for the audit log.
#:
#: The guarded properties are deliberately left **writable and configurable**. Hardening
#: them cost far more than it bought: a module bundle is strict mode, so a page wrapping
#: `window.fetch` — which Sentry, DataDog, New Relic and most analytics SDKs do — got an
#: uncaught TypeError and died. Measured: Ashby's bundle assigns `window.fetch` on line 33
#: of its entry chunk, so 15 of 34 real ATS postings rendered a 0-character DOM and the
#: harness then reported its own damage as "fewer than 2 real fields". Interception is what
#: provides the safety; non-writability only defended against a page racing to *unwrap* the
#: guard, which no real page does and which a fresh iframe would defeat anyway. Wrapping
#: preserves the guard: a page that wraps our `fetch` still calls through to it.
SAFETY_JS = """(() => {
  const marker = Symbol.for('browser-harness.dry-run');
  if (window[marker]) return true;
  const attempts = [];
  // authBudget is deliberately separate from the ordinary dry-run switch.  A caller may
  // authorize one narrowly validated authentication request without opening a route to
  // application submission.  The budget belongs to this document and expires after the
  // click helper returns.
  const state = {attempts, armed: false, authBudget: 0};
  const record = (kind, detail = {}) => {
    attempts.push({kind, detail, ts: Date.now()});
    console.warn('browser-harness dry-run policy blocked', kind, detail);
    return false;
  };
  Object.defineProperty(window, marker, {
    value: state, configurable: false, writable: false});
  const visible = el => !!(el && el.getClientRects && el.getClientRects().length);
  const authContext = () => {
    try {
      const passwords = [...document.querySelectorAll('input[type=password]')].filter(visible);
      const identities = [...document.querySelectorAll(
        'input[type=email],input[autocomplete=email],input[autocomplete=username]')].filter(visible);
      const applicationFiles = document.querySelectorAll('input[type=file]').length;
      const applicationText = [...document.querySelectorAll('textarea')].some(visible);
      const dataFields = [...document.querySelectorAll('input,select,textarea')].filter(el => {
        const type = String(el.type || '').toLowerCase();
        return visible(el) && !['hidden', 'submit', 'button', 'reset', 'image'].includes(type);
      });
      return !applicationFiles && !applicationText && dataFields.length <= 3
        && (passwords.length > 0 || identities.length > 0);
    } catch (err) { return false; }
  };
  const consumeAuth = () => {
    if (state.authBudget !== 1 || !authContext()) return false;
    state.authBudget = 0;
    return true;
  };
  document.addEventListener('submit', event => {
    // A submit event is not itself a network request.  Let an authorised auth form reach
    // its own handler while preserving the single request budget for the fetch/XHR that
    // handler normally performs.  A native navigation replaces this document and budget.
    if (state.authBudget === 1 && authContext()) return;
    record('form.submit', {action: event.target && event.target.action || ''});
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);
  const blockedSubmit = function() {
    return record('form.method', {action: this && this.action || ''});
  };
  for (const name of ['submit', 'requestSubmit']) {
    Object.defineProperty(HTMLFormElement.prototype, name, {
      value: blockedSubmit, configurable: true, writable: true});
  }
  // A POST is only dangerous once the page holds data somebody entered. Before that there
  // is nothing of the applicant's to send, so a boot-time POST cannot submit an
  // application — and blocking it merely stops the page rendering. Measured: 15 of 34 real
  // ATS postings (every Ashby, BambooHR and Workable one) served a 0-character DOM because
  // their SPA POSTs for its own content and got NotAllowedError back.
  //
  // The trigger is the page's own state rather than a flag the harness has to set, so this
  // costs no round trip: a control whose value differs from its default, or a file input
  // holding a file, means data is present. Errors fail CLOSED.
  // Form submit/requestSubmit and beacons stay blocked unconditionally — that is the real
  // send path, and it is refused whether data is present or not.
  // The writer runs in the isolated world, whose expandos do not cross realms — but DOM
  // *attributes* are shared document state, so the fill marks the document there and this
  // guard reads it, costing no extra round trip. `defaultValue` cannot be used for this:
  // React mirrors a programmatic write onto the value attribute, so value === defaultValue
  // even after the field was filled (measured on Personio: first_name read back
  // value="Enes Poyraz", defaultValue="Enes Poyraz").
  const dirty = () => {
    if (state.armed) return true;
    try {
      if (document.documentElement.hasAttribute('data-bh-entered')) return true;
      for (const el of document.querySelectorAll('input, textarea')) {
        if (el.type === 'file' && el.files && el.files.length) return true;
      }
      return false;
    } catch (err) { return true; }
  };
  const mutating = method => !['GET', 'HEAD', 'OPTIONS'].includes(
    String(method || 'GET').toUpperCase()) && dirty();
  const nativeFetch = window.fetch && window.fetch.bind(window);
  if (nativeFetch) Object.defineProperty(window, 'fetch', {
    configurable: true, writable: true,
    value: function(input, init = {}) {
      const method = init.method || (input && input.method) || 'GET';
      if (mutating(method) && !consumeAuth()) {
        record('fetch', {method: String(method).toUpperCase(), url: String(input && input.url || input)});
        return Promise.reject(new DOMException('blocked by browser-harness dry-run policy', 'NotAllowedError'));
      }
      return nativeFetch(input, init);
    }});
  const nativeOpen = XMLHttpRequest.prototype.open;
  const nativeSend = XMLHttpRequest.prototype.send;
  Object.defineProperty(XMLHttpRequest.prototype, 'open', {
    configurable: true, writable: true,
    value: function(method, url, ...rest) {
      this.__bhMethod = String(method || 'GET').toUpperCase();
      this.__bhUrl = String(url || '');
      return nativeOpen.call(this, method, url, ...rest);
    }});
  Object.defineProperty(XMLHttpRequest.prototype, 'send', {
    configurable: true, writable: true,
    value: function(body) {
      if (mutating(this.__bhMethod) && !consumeAuth()) {
        record('xhr', {method: this.__bhMethod, url: this.__bhUrl});
        throw new DOMException('blocked by browser-harness dry-run policy', 'NotAllowedError');
      }
      return nativeSend.call(this, body);
    }});
  if (navigator.sendBeacon) Object.defineProperty(navigator, 'sendBeacon', {
    configurable: true, writable: true,
    value: function(url) { return record('beacon', {url: String(url || '')}); }});
  return true;
})()"""

_DANGER_JS = """(el => {
  if (!el) return null;
  const control = el.closest && el.closest('button,input');
  if (!control) return {danger: false};
  const tag = control.tagName.toLowerCase();
  const type = String(control.type || '').toLowerCase();
  const form = control.form || (control.closest && control.closest('form'));
  const danger = tag === 'input' ? ['submit', 'image'].includes(type)
    : tag === 'button' && !!form && (!type || type === 'submit');
  return {danger, tag, type: type || null,
          label: String(control.innerText || control.value || '').trim().slice(0, 100),
          action: form && form.action || ''};
})"""

_AUTH_ACTION_JS = """(el => {
  if (!el) return null;
  const control = el.closest && el.closest('button,input,[role=button]');
  if (!control) return {allowed: false, reason: 'not_a_control'};
  const label = String(control.innerText || control.value ||
    control.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
  const authLabel = /(?:create account|sign up|register|sign in|log in|forgot .*password|reset .*password|verify .*email)/i.test(label);
  const visible = node => !!(node && node.getClientRects && node.getClientRects().length);
  const passwords = [...document.querySelectorAll('input[type=password]')].filter(visible);
  const identities = [...document.querySelectorAll(
    'input[type=email],input[autocomplete=email],input[autocomplete=username]')].filter(visible);
  const applicationFiles = document.querySelectorAll('input[type=file]').length;
  const applicationText = [...document.querySelectorAll('textarea')].some(visible);
  const dataFields = [...document.querySelectorAll('input,select,textarea')].filter(node => {
    const type = String(node.type || '').toLowerCase();
    return visible(node) && !['hidden', 'submit', 'button', 'reset', 'image'].includes(type);
  });
  const r = control.getBoundingClientRect();
  const allowed = authLabel && !applicationFiles && !applicationText && dataFields.length <= 3 &&
    (passwords.length > 0 || identities.length > 0);
  return {allowed, reason: allowed ? 'auth_context' : 'context_refused', label,
          password_fields: passwords.length, identity_fields: identities.length,
          application_files: applicationFiles, application_text: applicationText,
          data_fields: dataFields.length,
          x: r.x + r.width / 2, y: r.y + r.height / 2,
          url: location.href, mutations: window.__bh ? __bh.mutations : 0};
})"""

#: Extension -> MIME for reading a file input's `accept`. Only the types upload controls
#: realistically advertise; anything else is treated as admissible, because the job here
#: is to name a definite client-side rejection, never to invent one.
_MIME = {".pdf": "application/pdf", ".doc": "application/msword",
         ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         ".txt": "text/plain", ".rtf": "application/rtf",
         ".odt": "application/vnd.oasis.opendocument.text",
         ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".gif": "image/gif", ".webp": "image/webp"}


def _resolve_js(ref: str) -> str:
    """JS that resolves `ref` as a snapshot ref, falling back to a CSS selector.

    Refs come from `snapshot()`, which only registers elements that have a box. A file
    input almost never does: the standard pattern is a `display:none` input behind a
    styled dropzone, so `upload_file` was unreachable for exactly the case it exists to
    serve — every ATS, and joblens' own CV field. Accepting a selector costs one `||` and
    removes the need to drop to raw CDP.

    `querySelector` throws on a non-selector string (a bare ref id like `e12`), so the
    fallback is guarded — an unregistered ref must read as "not found", not as an error.
    """
    return (f"((window.__bh && window.__bh.refs && window.__bh.refs[{ref!r}])"
            f" || (() => {{ try {{ return document.querySelector({ref!r}); }}"
            f" catch (e) {{ return null; }} }})())")


def _accepts(accept: str, path: str) -> bool:
    """Would this input's `accept` admit this file? An empty filter or an unknown
    extension admits — a false rejection would be worse than no check at all."""
    if not accept.strip():
        return True
    ext = Path(path).suffix.lower()
    mime = _MIME.get(ext)
    for tok in (t.strip().lower() for t in accept.split(",")):
        if not tok:
            continue
        if tok.startswith("."):
            if tok == ext:
                return True
        elif tok.endswith("/*"):
            if mime and mime.startswith(tok[:-1]):
                return True
        elif mime and tok == mime:
            return True
    return mime is None


#: One in-page pass over the interactive elements (item 20). Coordinates are viewport CSS
#: pixels from getBoundingClientRect — exactly what Input.dispatchMouseEvent takes.
SNAPSHOT_JS = """(() => {
  const bh = window.__bh || (window.__bh = {refs: {}, n: 0, mutations: 0});
  const sel = 'a[href],button,input,select,textarea,[role=button],[role=link],' +
    '[role=checkbox],[role=radio],[role=combobox],[role=menuitem],[role=tab],' +
    '[onclick],[contenteditable=true]';
  const out = [];
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const invisible = !bh.visible(el);
    // A file input is never clicked — clicking opens a native picker that blocks the
    // renderer with no CDP way back out — so upload_file() always drives it
    // programmatically. Visibility therefore says nothing about whether it is reachable,
    // and every dropzone UI hides the real input behind a styled div. Excluding it left
    // the ordinary ATS upload with no ref at all, so upload_file() silently took the
    // nearest visible file input instead.
    const isFile = el.tagName === 'INPUT' && el.type === 'file';
    if (invisible && !isFile) continue;
    const ref = bh.ref(el);
    const it = {ref, tag: el.tagName.toLowerCase(),
      name: (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder
             || el.name || '').trim().slice(0, 80),
      x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
      w: Math.round(r.width), h: Math.round(r.height)};
    if (el.disabled) it.disabled = true;
    if (el.type && el.type !== el.tagName.toLowerCase()) it.type = el.type;
    if (invisible) it.hidden_control = true;
    if (el.tagName === 'SELECT') it.options = el.options.length;
    out.push(it);
  }
  return out;
})()"""


#: Draw a labelled box over every snapshot ref, so a screenshot and the structured
#: element list share one index (set-of-mark). Injected from the isolated world, but the
#: nodes must live in the page's own DOM or the renderer would not paint them into the
#: capture; they are removed immediately afterwards.
#:
#: Why this exists: structured extraction and vision each fail where the other is strong.
#: A schema cannot see that a control is a 1x1 clipped decoy — it read back byte-identical
#: and submitted nothing. A screenshot cannot see 249 collapsed <option>s, and a model
#: reading coordinates off an image estimates them. Sharing an index removes the trade:
#: look at the picture, act on the ref.
ANNOTATE_JS = """((els) => {
  const prev = document.getElementById('__bh_marks');
  if (prev) prev.remove();
  const layer = document.createElement('div');
  layer.id = '__bh_marks';
  layer.style.cssText =
    'position:fixed;inset:0;z-index:2147483647;pointer-events:none;font:11px/1.2 ' +
    'ui-monospace,Menlo,monospace';
  for (const e of els) {
    if (!e.w || !e.h) continue;                       // hidden controls have no box to draw
    const b = document.createElement('div');
    b.style.cssText =
      `position:absolute;left:${e.x - e.w / 2}px;top:${e.y - e.h / 2}px;` +
      `width:${e.w}px;height:${e.h}px;outline:2px solid #e0115f;` +
      'outline-offset:-1px;background:rgba(224,17,95,.06)';
    const tag = document.createElement('span');
    tag.textContent = e.ref;
    tag.style.cssText =
      'position:absolute;left:0;top:-14px;padding:0 3px;background:#e0115f;' +
      'color:#fff;border-radius:2px;white-space:nowrap';
    b.appendChild(tag);
    layer.appendChild(b);
  }
  document.documentElement.appendChild(layer);
  return els.length;
})(__ELS__)"""


#: Name of the isolated-world binding a watcher calls when its condition becomes true.
#: Scoped to WORLD via `executionContextName`, NOT global: an unscoped `Runtime.addBinding`
#: puts a function on the page's own `window`, which is the exact detectability leak the
#: isolated world was introduced to close.
BINDING = "__bhNotify"

#: Wait for a selector without polling (D13). Evaluates once, and only if that misses does
#: it arm a MutationObserver that re-checks and fires the binding. My own live checks are
#: littered with `time.sleep(1.0)` because this did not exist — a guessed sleep is both
#: slower than it needs to be and wrong when the page is slower than the guess.
WATCH_JS = """((sel, state, token) => {
  const bh = window.__bh;
  const ok = () => {
    const e = document.querySelector(sel);
    if (state === 'gone') return !e;
    if (!e) return false;
    return state !== 'visible' || bh.visible(e);
  };
  if (ok()) return {matched: true, immediate: true};
  bh.watch = bh.watch || {};
  const obs = new MutationObserver(() => {
    if (!ok()) return;
    obs.disconnect();
    delete bh.watch[token];
    __bhNotify(token);
  });
  obs.observe(document.documentElement || document,
    {subtree: true, childList: true, attributes: true, characterData: true});
  bh.watch[token] = obs;
  return {matched: false, immediate: false};
})(__SEL__, __STATE__, __TOKEN__)"""

#: Wait for a *form*, not for a selector — the question every caller was actually asking.
#:
#: A blanket `input, textarea, form, main` selector answers a different question, and
#: measurement showed it answering it wrongly in both directions: 27% of 64 live
#: `wait_for` calls timed out (15s p95) on pages where nothing would ever match, and on
#: pages with a cookie banner it matched the banner's checkbox in ~6ms and reported
#: success while the real form was still seconds away. Neither branch ever waited for the
#: right thing.
#:
#: So the condition is a count of controls that could hold applicant data: laid out,
#: not display:none, not a submit button, not site furniture. Same exclusions as
#: `_SCHEMA_JS`, deliberately — a wait that resolves on controls the schema then discards
#: is the cookie-banner false positive rebuilt one layer down.
WATCH_FORM_JS = """((minFields, token) => {
  const bh = window.__bh;
  const count = () => {
    let n = 0;
    for (const el of document.querySelectorAll('input,select,textarea,[contenteditable=true]')) {
      const type = (el.type || '').toLowerCase();
      if (['submit', 'button', 'reset', 'image', 'hidden', 'search'].includes(type)) continue;
      if (bh.furniture(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.width <= 2 && r.height <= 2) continue;
      if (el.offsetParent === null && getComputedStyle(el).position !== 'fixed') continue;
      n++;
    }
    return n;
  };
  const now = count();
  if (now >= minFields) return {matched: true, immediate: true, fields: now};
  bh.watch = bh.watch || {};
  const obs = new MutationObserver(() => {
    if (count() < minFields) return;
    obs.disconnect();
    delete bh.watch[token];
    __bhNotify(token);
  });
  obs.observe(document.documentElement || document,
    {subtree: true, childList: true, attributes: true, characterData: true});
  bh.watch[token] = obs;
  return {matched: false, immediate: false, fields: now};
})(__MIN__, __TOKEN__)"""

#: Read the same counts back without arming an observer, for the final report.
FORM_COUNTS_JS = """(() => {
  const all = document.querySelectorAll('input,textarea,select');
  return [all.length,
          [...all].filter(e => e.offsetParent !== null).length,
          ((document.body && document.body.innerText) || '').trim().length];
})()"""

#: The keyboard twin of `_activate_click`'s DOM fallback. `__PRE__` is the `__bh.keys`
#: reading taken before the trusted events were dispatched; if the counter has not moved,
#: the renderer dropped every one of them (it does exactly that for a tab that is not its
#: window's selected tab — measured: 0 of 2 keydowns seen by page listeners, no CDP error)
#: and the keystrokes are synthesized through the DOM instead: per character, a keydown
#: the page's handlers can see, the value change a real keystroke would have made, an
#: InputEvent, a keyup. `isTrusted` is false on the synthetic path — a widget that insists
#: on trusted events cannot be driven in a background tab by anyone; the alternative is
#: what was measured, keystrokes that silently go nowhere.
#:
#: The counter read is race-free: Input.dispatchKeyEvent does not ACK until the renderer
#: has processed the event (the same property the dialog dance is built on), so by the
#: time this evaluates, a delivered keydown has already been counted.
_SYNTH_KEYS_JS = """/* bh-synth-keys */ ((pre, text, ref) => {
  const bh = window.__bh || {keys: 0, refs: {}};
  const delivered = (bh.keys || 0) - pre;
  if (delivered > 0) return {delivered, synthesized: false};
  const el = (ref && bh.refs && bh.refs[ref]) || document.activeElement;
  if (!el || el === document.body || el === document.documentElement)
    return {delivered: 0, synthesized: false, error: 'no_target'};
  const editable = el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement;
  let desc = null;
  if (editable) {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
                                                    : HTMLInputElement.prototype;
    desc = Object.getOwnPropertyDescriptor(proto, 'value');
    // A real keystroke replaces the selection; appending would concatenate instead
    // (the write path select()s the field first, expecting replacement).
    if (el.selectionStart !== el.selectionEnd) {
      const v = String(el.value);
      const keep = v.slice(0, el.selectionStart) + v.slice(el.selectionEnd);
      if (desc && desc.set) desc.set.call(el, keep); else el.value = keep;
    }
  }
  for (const ch of String(text)) {
    el.dispatchEvent(new KeyboardEvent('keydown', {key: ch, bubbles: true, cancelable: true}));
    if (editable) {
      const v = String(el.value) + ch;
      if (desc && desc.set) desc.set.call(el, v); else el.value = v;
      el.dispatchEvent(new InputEvent('input', {data: ch, inputType: 'insertText', bubbles: true}));
    } else if (el.isContentEditable) {
      el.textContent = String(el.textContent) + ch;
      el.dispatchEvent(new InputEvent('input', {data: ch, inputType: 'insertText', bubbles: true}));
    }
    el.dispatchEvent(new KeyboardEvent('keyup', {key: ch, bubbles: true}));
  }
  return {delivered: 0, synthesized: true};
})(__PRE__, __TEXT__, __REF__)"""

#: Same contract for a single named key (Escape, Enter, Tab...). Browser DEFAULT ACTIONS
#: do not run on a synthetic event — Tab will not move focus, Enter will not submit (the
#: dry-run guard refuses submission anyway) — but page handlers do, and page handlers are
#: what Escape-closes-the-popup and Enter-confirms-the-option are made of.
_SYNTH_KEY_JS = """/* bh-synth-key */ ((pre, key, code, text, mods) => {
  const bh = window.__bh || {keys: 0};
  if ((bh.keys || 0) - pre > 0) return {synthesized: false};
  const el = document.activeElement || document.body || document.documentElement;
  const init = {key, code, bubbles: true, cancelable: true,
                altKey: !!(mods & 1), ctrlKey: !!(mods & 2),
                metaKey: !!(mods & 4), shiftKey: !!(mods & 8)};
  el.dispatchEvent(new KeyboardEvent('keydown', init));
  if (text && !mods
      && (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement)) {
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
                                                    : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    const v = String(el.value) + text;
    if (desc && desc.set) desc.set.call(el, v); else el.value = v;
    el.dispatchEvent(new InputEvent('input', {data: text, inputType: 'insertText', bubbles: true}));
  }
  el.dispatchEvent(new KeyboardEvent('keyup', init));
  return {synthesized: true};
})(__PRE__, __KEY__, __CODE__, __TEXT__, __MODS__)"""

#: And for the wheel. The fallback scrolls what a wheel at (x, y) would have scrolled:
#: the nearest scrollable ancestor of the element under the point, else the window.
_SYNTH_SCROLL_JS = """/* bh-synth-scroll */ ((pre, x, y, dx, dy) => {
  const bh = window.__bh || {scrolls: 0};
  let modality = 'compositor';
  if ((bh.scrolls || 0) - pre === 0) {
    modality = 'dom';
    let el = document.elementFromPoint(x, y);
    let scrolled = false;
    while (el && el !== document.body && el !== document.documentElement) {
      const cs = getComputedStyle(el);
      if ((el.scrollHeight > el.clientHeight || el.scrollWidth > el.clientWidth)
          && /(auto|scroll|overlay)/.test(cs.overflowY + ' ' + cs.overflowX)) {
        el.scrollBy(dx, dy);
        scrolled = true;
        break;
      }
      el = el.parentElement;
    }
    if (!scrolled) window.scrollBy(dx, dy);
  }
  return {y: Math.round(scrollY), height: document.documentElement.scrollHeight,
          atBottom: Math.ceil(scrollY + innerHeight) >= document.documentElement.scrollHeight,
          modality};
})(__PRE__, __X__, __Y__, __DX__, __DY__)"""

#: Wait for the page to reach a meaningful application state, not merely for `load`.
#:
#: Client-rendered ATS pages commonly set `document.title` from server metadata before
#: React has mounted anything into `<body>`.  Treating that transient 0/0 snapshot as a
#: bot wall lost 9 of 11 Ashby forms in a ten-tab run.  This probe reports strong terminal
#: states immediately and otherwise arms a one-shot mutation observer; Python decides
#: whether a quiet usable page or quiet empty page has been stable long enough.
WATCH_APPLICATION_STATE_JS = """((token) => {
  const bh = window.__bh;
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

  bh.watch = bh.watch || {};
  if (bh.watch[token]) bh.watch[token].disconnect();
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


def _unwrap_eval(r: dict[str, Any]) -> Any:
    """`Runtime.evaluate` result → a Python value, or the typed error. One implementation,
    shared by the main-world and isolated-world paths."""
    if ex := r.get("exceptionDetails"):
        desc = (ex.get("exception") or {}).get("description") or ex.get("text", "")
        raise JsException(desc.split("\n")[0][:300], line=ex.get("lineNumber"),
                          url=ex.get("url"), stack=desc[:1000])
    res = r.get("result") or {}
    if "value" in res or res.get("type") == "undefined":
        return res.get("value")
    # rule 3: a value we cannot hand over is an error, not a silent None (v1's bug)
    raise NotSerializable(
        f"result of type {res.get('subtype') or res.get('type')} has no JSON value",
        type=res.get("type"), description=(res.get("description") or "")[:120])


def _count(value):
    """A delivery-counter reading, defensively. A page that answers the probe with
    anything but a number contributes 0, so the fallback can still fire on the delta."""
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


#: keyName -> (code, text). Only printable keys carry text; see press_key.
_KEYS = {
    "Enter": ("Enter", "\r"), "Tab": ("Tab", "\t"), "Backspace": ("Backspace", ""),
    "Escape": ("Escape", ""), "Delete": ("Delete", ""), " ": ("Space", " "),
    "ArrowLeft": ("ArrowLeft", ""), "ArrowRight": ("ArrowRight", ""),
    "ArrowUp": ("ArrowUp", ""), "ArrowDown": ("ArrowDown", ""),
    "Home": ("Home", ""), "End": ("End", ""), "PageUp": ("PageUp", ""),
    "PageDown": ("PageDown", ""),
}


class _Waiter:
    """Buffers matching events from arming time, so nothing that fires between `navigate`
    returning and the wait starting can be missed."""

    __slots__ = ("cond", "hits", "pred")

    def __init__(self, pred):
        self.pred = pred
        self.cond = threading.Condition()
        self.hits: list[tuple[float, dict[str, Any]]] = []

    def offer(self, msg: dict[str, Any]) -> None:
        try:
            if not self.pred(msg):
                return
        except Exception:  # noqa: BLE001 — a bad predicate must not kill the reader
            return
        with self.cond:
            self.hits.append((time.perf_counter(), msg))
            self.cond.notify_all()

    def wait_match(self, pred, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                for _, m in self.hits:
                    if pred(m):
                        return m
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self.cond.wait(left)


class Tab:
    """Primitives bound to one target. All CDP goes through the target's registered
    session; all waits go through one subscriber registered at construction."""

    def __init__(self, conn: Connection, registry: SessionRegistry, target_id: str, *,
                 journal: Journal | None = None, accept_dialogs: bool = False):
        self._conn, self._reg, self.target_id = conn, registry, target_id
        self._j = journal or conn.journal
        self.accept_dialogs = accept_dialogs
        self._session_id: str | None = None
        self._wlock = threading.Lock()
        self._waiters: list[_Waiter] = []
        self._dialog: dict[str, Any] | None = None
        self._created: deque[dict[str, Any]] = deque(maxlen=16)
        self._diagnostic_events: deque[dict[str, Any]] = deque(maxlen=128)
        self._diagnostics_enabled = False
        self._diagnostics_started = 0.0
        self._world_ctx: int | None = None
        self._bound = False
        conn.subscribe(self._on_event)
        try:
            self._install_runtime()
        except Exception:
            conn.unsubscribe(self._on_event)
            raise

    def close(self) -> None:
        self._conn.unsubscribe(self._on_event)

    @property
    def journal(self) -> Journal:
        return self._j

    # -- plumbing ----------------------------------------------------------

    def _sid(self) -> str:
        sid = self._reg.ensure_live(self.target_id).session_id
        if self._session_id is not None and sid != self._session_id:
            # The registry recovered a stale session by re-attaching (a session is a
            # lease; the target is the identity). Everything the OLD session carried is
            # gone with it — injected-script registrations, the runtime binding, event
            # subscriptions — and none of it announces its own absence: the next
            # navigation would simply load without the dry-run guard, and waits would
            # quietly run to their timeouts. So a changed session id re-arms it all,
            # here, before the call that noticed the change proceeds.
            self._session_id = sid
            self._world_ctx = None
            self._bound = False
            self._rearm_session(sid)
        else:
            self._session_id = sid
        return sid

    def _rearm_session(self, sid: str) -> None:
        """Reinstall per-session state on a replacement session, quietly but journaled."""
        self._j.write("note", event="session_rearmed", target_id=self.target_id)
        self._register_runtime(sid)
        self._ensure_world()

    def _register_runtime(self, sid: str) -> None:
        """The one place the injected scripts are registered — first attach and every
        session recovery go through the same two calls, so they cannot drift apart.
        SAFETY_JS is a safety property; a drifted copy that forgot it would announce
        nothing."""
        self._conn.request(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": SAFETY_JS, "runImmediately": True},
            session_id=sid, timeout=10.0)
        self._conn.request(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": RUNTIME_JS, "worldName": WORLD, "runImmediately": True},
            session_id=sid, timeout=10.0)

    def _install_runtime(self) -> None:
        """Item 18: the registry + mutation counter exist on every document this tab will
        ever load, so refs survive navigation by reinstallation, not by luck — and they
        live in an isolated world, so the page never sees them."""
        self._register_runtime(self._sid())
        self._ensure_world()                                   # and for the current document

    def _ensure_world(self) -> int | None:
        """Isolated-world context id for the main frame, created on demand.

        Worlds die with their document, so this is re-resolved rather than cached across
        navigations; `executionContextsCleared` drops the stale id (see `_on_event`).
        """
        if self._world_ctx is not None:
            return self._world_ctx
        sid = self._sid()
        try:
            frame = self._conn.request("Page.getFrameTree", session_id=sid,
                                       timeout=10.0)["frameTree"]["frame"]["id"]
            ctx = self._conn.request(
                "Page.createIsolatedWorld",
                {"frameId": frame, "worldName": WORLD, "grantUniveralAccess": True},
                session_id=sid, timeout=10.0)["executionContextId"]
        except HarnessError:
            return None            # degrade to the main world rather than fail the call
        self._conn.request("Runtime.evaluate",
                           {"expression": RUNTIME_JS, "contextId": ctx},
                           session_id=sid, timeout=10.0)
        if not self._bound:
            try:
                # executionContextName scopes the binding to the isolated world, so the
                # page's own `window` never gains a `__bhNotify` to detect.
                self._conn.request("Runtime.addBinding",
                                   {"name": BINDING, "executionContextName": WORLD},
                                   session_id=sid, timeout=10.0)
                self._bound = True
            except HarnessError:
                pass                  # waits fall back to their timeout, nothing else breaks
        self._world_ctx = ctx
        return ctx

    def arm_dry_run(self) -> bool:
        """Close the read-only window: from here on mutating fetch/XHR are refused.

        Called by the first helper that writes applicant data into the page. Until then the
        document holds nothing of ours and a POST cannot send an application; afterwards
        every mutating request is a candidate for exactly that. Form submission, beacons
        and `requestSubmit` stay blocked either way — this only governs XHR/fetch.
        """
        try:
            return bool(self.js(
                "(() => {const s = window[Symbol.for('browser-harness.dry-run')];"
                " if (!s) return false; s.armed = true; return true;})()", timeout=5.0))
        except HarnessError:
            return False               # a page we cannot reach cannot send anything either

    def _world_js(self, expression: str, *, timeout: float = 10.0) -> Any:
        """Evaluate harness machinery in the isolated world. Falls back to the main world
        only if the world could not be created, so a degraded run still works."""
        ctx = self._ensure_world()
        if ctx is None:
            return self.js(expression, timeout=timeout)
        # replMode here too, for the same reason js() needs it: `fetch_all`'s template is a
        # top-level `await`, which is a syntax error without it (D14).
        params = {"expression": expression, "returnByValue": True, "awaitPromise": True,
                  "replMode": True, "contextId": ctx}
        try:
            r = self.cdp("Runtime.evaluate", params, timeout=timeout)
        except HarnessError as e:
            if e.cls is not Class.CDP_ERROR:
                raise
            self._world_ctx = None                 # context died under us; rebuild once
            ctx = self._ensure_world()
            if ctx is None:
                return self.js(expression, timeout=timeout)
            params["contextId"] = ctx
            r = self.cdp("Runtime.evaluate", params, timeout=timeout)
        return _unwrap_eval(r)

    def _on_event(self, msg: dict[str, Any]) -> None:
        """Reader thread: bookkeeping and waiter wakeups only, never a request."""
        sid = msg.get("sessionId")
        if sid is not None and sid != self._session_id:
            return                                     # another tab's event
        method = msg.get("method", "")
        params = msg.get("params") or {}
        if self._diagnostics_enabled:
            diagnostic = self._sanitize_diagnostic_event(method, params)
            if diagnostic is not None:
                self._diagnostic_events.append(diagnostic)
        if method in ("Runtime.executionContextsCleared", "Page.frameNavigated"):
            self._world_ctx = None            # the world died with its document
        if method == "Page.javascriptDialogOpening":
            with self._wlock:
                self._dialog = params
            # A page may install a beforeunload handler as soon as applicant data changes.
            # Chrome then blocks Page.navigate/Target.close behind its native "Leave
            # site?" prompt.  It is not an application action; accepting it only permits
            # the navigation the caller already requested.  Handle this one dialog type
            # immediately off the reader thread. Alerts and confirms retain the normal
            # click-dialog policy below, including accept_dialogs=False by default.
            if params.get("type") == "beforeunload":
                threading.Thread(
                    target=self._accept_beforeunload,
                    args=(params,),
                    name="bh-beforeunload",
                    daemon=True,
                ).start()
        elif method == "Target.targetCreated":
            info = params.get("targetInfo") or {}
            if info.get("type") == "page" and info.get("targetId") != self.target_id:
                self._created.append(info)
        with self._wlock:
            waiters = list(self._waiters)
        for w in waiters:
            w.offer(msg)

    def _accept_beforeunload(self, pending: dict[str, Any]) -> None:
        try:
            self.cdp("Page.handleJavaScriptDialog", {"accept": True}, timeout=5.0)
        except HarnessError as exc:
            # Chrome can close the dialog itself between the event and our command.
            if "No dialog is showing" not in str(exc):
                self._j.write("note", event="beforeunload_handle_failed",
                              error_class=exc.cls.value)
        finally:
            with self._wlock:
                if self._dialog is pending:
                    self._dialog = None

    def _sanitize_diagnostic_event(self, method: str,
                                   params: dict[str, Any]) -> dict[str, Any] | None:
        """Keep failure shape and lifecycle, never URLs, text, headers, or bodies."""
        out: dict[str, Any] = {"method": method, "offset_ms": round(
            (time.time() - self._diagnostics_started) * 1000, 1)}
        if method == "Network.loadingFailed":
            text = str(params.get("errorText") or "")
            out.update({"type": params.get("type"), "cancelled": bool(params.get("canceled")),
                        "blocked_reason": params.get("blockedReason"),
                        "error_sha256": hashlib.sha256(text.encode()).hexdigest()[:16]})
        elif method == "Network.responseReceived":
            response = params.get("response") or {}
            status = int(response.get("status") or 0)
            if status < 400:
                return None
            out.update({"type": params.get("type"), "status": status,
                        "mime_type": response.get("mimeType")})
        elif method == "Runtime.exceptionThrown":
            detail = params.get("exceptionDetails") or {}
            text = str(detail.get("text") or (detail.get("exception") or {}).get("className") or "")
            out.update({"line": detail.get("lineNumber"), "column": detail.get("columnNumber"),
                        "exception_sha256": hashlib.sha256(text.encode()).hexdigest()[:16]})
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            text = str(entry.get("text") or "")
            out.update({"level": entry.get("level"), "source": entry.get("source"),
                        "text_sha256": hashlib.sha256(text.encode()).hexdigest()[:16]})
        elif method in {"Inspector.targetCrashed", "Target.targetCrashed",
                        "Target.detachedFromTarget", "Page.frameDetached",
                        "Page.frameNavigated"}:
            out["reason"] = params.get("reason")
        else:
            return None
        return out

    def start_diagnostics(self) -> dict[str, Any]:
        """Enable bounded, privacy-safe evidence before a navigation."""
        self._diagnostic_events.clear()
        self._diagnostics_started = time.time()
        enabled = []
        for method in ("Runtime.enable", "Network.enable", "Log.enable", "Performance.enable"):
            try:
                self.cdp(method, timeout=5.0)
                enabled.append(method.split(".", 1)[0])
            except HarnessError:
                pass
        self._diagnostics_enabled = True
        return {"enabled": enabled, "event_limit": self._diagnostic_events.maxlen}

    def diagnostics(self) -> dict[str, Any]:
        """Snapshot lifecycle, failures, resources, performance, and event-loop delay."""
        started = time.perf_counter()
        metrics: dict[str, float] = {}
        try:
            raw = self.cdp("Performance.getMetrics", timeout=5.0).get("metrics") or []
            keep = {"Timestamp", "Documents", "Frames", "JSEventListeners", "Nodes",
                    "LayoutCount", "RecalcStyleCount", "ScriptDuration", "TaskDuration",
                    "JSHeapUsedSize", "JSHeapTotalSize"}
            metrics = {str(row.get("name")): float(row.get("value") or 0)
                       for row in raw if row.get("name") in keep}
        except HarnessError:
            pass
        try:
            resources = self._world_js("""(() => {
              const rows = performance.getEntriesByType('resource');
              const kinds = {}; let transfer = 0; let longest = 0;
              for (const r of rows) { const k = r.initiatorType || 'other';
                kinds[k] = (kinds[k] || 0) + 1; transfer += r.transferSize || 0;
                longest = Math.max(longest, r.duration || 0); }
              return {count: rows.length, by_type: kinds, transfer_bytes: transfer,
                      longest_ms: Math.round(longest)};
            })()""", timeout=5.0) or {}
        except HarnessError:
            resources = {}
        try:
            event_loop_ms = float(self._world_js("""await new Promise(resolve => {
              const start = performance.now(); setTimeout(() => resolve(performance.now()-start), 0);
            })""", timeout=5.0) or 0)
        except (HarnessError, TypeError, ValueError):
            event_loop_ms = 0.0
        try:
            frame_tree = self.cdp("Page.getFrameTree", timeout=5.0).get("frameTree") or {}
            def count_frames(node: dict[str, Any]) -> int:
                return 1 + sum(count_frames(child) for child in node.get("childFrames") or [])
            frame_count = count_frames(frame_tree) if frame_tree else 0
        except HarnessError:
            frame_count = 0
        return {
            "events": list(self._diagnostic_events), "events_dropped":
                len(self._diagnostic_events) == self._diagnostic_events.maxlen,
            "metrics": metrics, "resources": resources, "frame_count": frame_count,
            "event_loop_delay_ms": round(event_loop_ms, 1),
            "capture_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    @contextmanager
    def _armed(self, pred):
        w = _Waiter(pred)
        with self._wlock:
            self._waiters.append(w)
        try:
            yield w
        finally:
            with self._wlock:
                self._waiters.remove(w)

    # -- item 15: the escape hatches --------------------------------------

    def cdp(self, method: str, params: dict[str, Any] | None = None, *,
            timeout: float = 20.0) -> dict[str, Any]:
        if method == "Network.replayXHR":
            raise SideEffectRefused(
                "replaying an XHR may repeat an irreversible request",
                method=method, target_id=self.target_id)
        if method == "Fetch.continueRequest":
            verb = str((params or {}).get("method") or "GET").upper()
            if verb not in ("GET", "HEAD", "OPTIONS") or (params or {}).get("postData"):
                raise SideEffectRefused(
                    "continuing a mutating intercepted request is disabled",
                    method=method, verb=verb, target_id=self.target_id)
        return self._conn.request(method, params, session_id=self._sid(), timeout=timeout)

    def js(self, expression: str, *, timeout: float = 10.0,
           await_promise: bool = True) -> Any:
        """Evaluate with `replMode`, so top-level `await` and re-declared `const` work
        (D14) — v1 grew a wrap-and-retry heuristic instead and mis-wrapped nested returns.

        Sharp edge, measured: under replMode a **bare async IIFE** `(async()=>{...})()`
        resolves to `{}` — awaitPromise is effectively ignored there. Write top-level
        `await (async()=>{...})()` instead; replMode handles the await natively.
        """
        with self._j.call("js", expression=expression[:200]):
            return self._main_js(expression, timeout=timeout, await_promise=await_promise)

    def _main_js(self, expression: str, *, timeout: float = 10.0,
                 await_promise: bool = True) -> Any:
        """Main-world evaluation for composite helpers that own their outer span."""
        r = self.cdp("Runtime.evaluate", {
            "expression": expression, "replMode": True, "returnByValue": True,
            "awaitPromise": await_promise}, timeout=timeout)
        return _unwrap_eval(r)

    # -- item 16 + 19: navigation and event-driven waits -------------------

    def goto(self, url: str, *, timeout: float = 20.0, wait_until: str = "load") -> dict[str, Any]:
        """Returns `{requested, landed, lifecycle}` or raises `NavigationFailed`/`Timeout`.
        A 404 error page cannot be reported as a title (v1 did exactly that).

        `lifecycle` says which condition ended the wait, and there are three:

        | value       | meaning |
        |-------------|---------|
        | `"load"`    | the requested event arrived — the normal case |
        | `"settled"` | it did not, but the document parsed and the network went quiet |
        | `"timeout"` | neither, yet the document is demonstrably usable |

        The last two exist because *one stalled subresource holds `load` forever*. A single
        image, stylesheet or iframe that is accepted and never answered is enough: measured
        on all three, `DOMContentLoaded` fires, the form is in the DOM and fillable, paint
        completes — and `load` never comes. The old code waited out the full timeout and
        then raised, discarding a page that had been ready for seconds.

        That is not hypothetical. Across three live runs every single `goto` failure — 14
        of 14, 505 seconds — was one host tarpitting its subresources. Those pages were
        usable: the caller caught the `Timeout`, carried on, and filled the forms anyway.
        The harness spent 505 seconds proving the caller right.

        `settled` is safe to accept early because of the *order* the events arrive in. On a
        healthy page Chrome emits `DOMContentLoaded` → `load` → `networkAlmostIdle`, so
        `load` wins the race and nothing changes. Only when `load` is absent does the pair
        arrive without it, which is precisely the stalled case.
        """
        seen: set[str] = set()

        def satisfied(msg: dict[str, Any]) -> bool:
            # Only ever called from `wait_match`, which runs after `loader` is assigned —
            # so the buffered events from BEFORE this navigation are filtered here rather
            # than being allowed to accumulate into `seen` and satisfy the pair rule with
            # the previous document's events.
            p = msg.get("params") or {}
            name = p.get("name")
            if not isinstance(name, str):
                return False
            if not loader:
                return name == wait_until      # unidentifiable: exact match only
            if p.get("loaderId") != loader:
                return False                   # another navigation's lifecycle, not ours
            seen.add(name)
            if name == wait_until:
                return True
            return wait_until == "load" and {"DOMContentLoaded",
                                             "networkAlmostIdle"} <= seen

        loader = None
        with self._j.call("goto", url=url), \
             self._armed(lambda m: m.get("method") == "Page.lifecycleEvent") as w:
            nav = self.cdp("Page.navigate", {"url": url}, timeout=timeout)
            if err := nav.get("errorText"):
                raise NavigationFailed(err, requested=url, landed=self._try_url())
            loader = nav.get("loaderId")
            hit = w.wait_match(satisfied, timeout)
            got = (hit.get("params") or {}).get("name") if hit else None
            lifecycle = ("load" if got == wait_until
                         else "settled" if hit is not None else "timeout")
            if hit is None and not self._usable_document(timeout):
                raise Timeout(f"no {wait_until!r} lifecycle event in {timeout}s",
                              requested=url, wait_until=wait_until, lifecycle_seen=sorted(seen))
        landed = self._try_url() or url
        if landed.startswith("chrome-error://"):
            raise NavigationFailed("landed on an error page", requested=url, landed=landed)
        return {"requested": url, "landed": landed, "lifecycle": lifecycle}

    def _usable_document(self, timeout: float) -> bool:
        """Is there a real page here, whatever the lifecycle events say?

        The bar deliberately excludes `readyState === 'loading'`: a parser that has not
        finished may still be about to produce the form, and returning then would hand the
        caller a half-built document that reads as an empty page. Past that point, content
        or controls is enough to be worth returning.
        """
        try:
            state, controls, text = self.js(
                "[document.readyState,"
                " document.querySelectorAll('input,textarea,select,button').length,"
                " ((document.body && document.body.innerText) || '').trim().length]",
                timeout=min(timeout, 5.0))
        except HarnessError:
            return False
        return state != "loading" and (int(controls) > 0 or int(text) > 0)

    def wait_lifecycle(self, name: str = "networkIdle", *, timeout: float = 10.0) -> None:
        with self._armed(lambda m: m.get("method") == "Page.lifecycleEvent") as w:
            if w.wait_match(lambda m: (m.get("params") or {}).get("name") == name,
                            timeout) is None:
                raise Timeout(f"no {name!r} lifecycle event in {timeout}s", wait=name)

    def _try_url(self) -> str | None:
        try:
            return self.js("location.href", timeout=5.0)
        except HarnessError:
            return None

    # -- items 17 + 20: snapshot, refs, and clicks that report a delta ------

    def snapshot(self) -> list[dict[str, Any]]:
        """Interactive elements with viewport-CSS coordinates, one round trip (item 20)."""
        with self._j.call("snapshot"):
            return self._world_js(SNAPSHOT_JS) or []

    def click_ref(self, ref: str, *, settle: float = 0.15, timeout: float = 10.0) -> dict[str, Any]:
        pre = self._world_js(
            f"(() => {{const el = window.__bh && __bh.refs[{ref!r}]; if (!el) return null;"
            " el.scrollIntoView({block: 'center', inline: 'center'});"
            " const r = el.getBoundingClientRect();"
            f" return [r.x + r.width/2, r.y + r.height/2, location.href, __bh.mutations, ({_DANGER_JS})(el)];}})()",
            timeout=timeout)
        if pre is None:
            raise ElementGone(f"no element registered for ref {ref!r}", ref=ref)
        x, y, url_before, mut_before = pre[:4]
        danger = pre[4] if len(pre) > 4 else None
        self._refuse_danger(danger, ref=ref)
        return self._click(x, y, url_before, int(mut_before), settle, timeout, ref=ref)

    def click_at(self, x: float, y: float, *, settle: float = 0.15,
                 timeout: float = 10.0) -> dict[str, Any]:
        """Coordinate click — the default modality: compositor-level events pass through
        iframes and shadow roots that no selector can reach."""
        before = self._world_js(
            f"[location.href, window.__bh ? __bh.mutations : 0,"
            f" ({_DANGER_JS})(document.elementFromPoint({x!r}, {y!r}))]", timeout=timeout)
        self._refuse_danger(before[2] if len(before) > 2 else None, x=x, y=y)
        return self._click(x, y, before[0], int(before[1]), settle, timeout)

    def click_auth_ref(self, ref: str, *, settle: float = 0.6,
                       timeout: float = 15.0) -> dict[str, Any]:
        """Allow exactly one authentication request from a validated auth-only UI.

        This is not a generic submit override.  The target must be labelled as an account,
        login, recovery or verification action; the document must contain identity or
        password controls; and application text/file inputs must be absent.  The main-world
        guard consumes a one-request budget and the helper clears any unused budget.
        """
        pre = self._world_js(
            f"(() => {{const el = window.__bh && __bh.refs[{ref!r}];"
            f" return ({_AUTH_ACTION_JS})(el);}})()", timeout=timeout)
        if pre is None:
            raise ElementGone(f"no element registered for ref {ref!r}", ref=ref)
        if not isinstance(pre, dict) or not pre.get("allowed"):
            evidence = {"ref": ref, **(pre if isinstance(pre, dict) else {})}
            self._j.write("note", event="side_effect_refused", **evidence)
            raise SideEffectRefused("authentication action did not pass the scoped gate",
                                    **evidence)
        with self._j.call("click_auth_ref", ref=ref, label=pre.get("label")):
            # Background Chrome renderers can silently drop compositor clicks.  Ordinary
            # clicks may safely retry through DOM activation; an account action must never
            # be repeated, so bring this tab forward and deliver one physical click.
            self.cdp("Page.bringToFront", timeout=5.0)
            armed = bool(self.js(
                "(() => {const s=window[Symbol.for('browser-harness.dry-run')];"
                " if(!s) return false; s.authBudget=1; return true;})()", timeout=5.0))
            if not armed:
                raise SideEffectRefused("the document has no active dry-run guard", ref=ref)
            try:
                delta = self._click(float(pre["x"]), float(pre["y"]), str(pre["url"]),
                                    int(pre["mutations"]), settle, timeout, ref=ref,
                                    retry_inert=False)
            finally:
                try:
                    self.js("(() => {const s=window[Symbol.for('browser-harness.dry-run')];"
                            " if(s) s.authBudget=0; return true;})()", timeout=5.0)
                except HarnessError:
                    pass
        return {**delta, "auth_action": pre.get("label"), "request_budget": 1}

    def _refuse_danger(self, danger: Any, **observed: Any) -> None:
        if not isinstance(danger, dict) or not danger.get("danger"):
            return
        evidence = {**observed, **danger, "target_id": self.target_id}
        self._j.write("note", event="side_effect_refused", **evidence)
        raise SideEffectRefused(
            "submit controls are disabled by the browser-harness dry-run policy", **evidence)

    def _click(self, x: float, y: float, url_before: str, mut_before: int,
               settle: float, timeout: float, ref: str | None = None,
               retry_inert: bool = True) -> dict[str, Any]:
        interesting = ("Page.lifecycleEvent", "Page.frameNavigated",
                       "Page.javascriptDialogOpening", "Target.targetCreated")
        targets_before = len(self._created)
        with self._j.call("click", x=x, y=y, ref=ref) , \
             self._armed(lambda m: m.get("method") in interesting) as w:
            for kind in ("mousePressed", "mouseReleased"):
                try:
                    self.cdp("Input.dispatchMouseEvent",
                             {"type": kind, "x": x, "y": y, "button": "left",
                              "clickCount": 1}, timeout=min(timeout, 2.0))
                except Timeout:
                    with self._wlock:
                        blocked_by_dialog = self._dialog is not None
                    if not blocked_by_dialog:
                        raise          # a real hang, not the dialog dance
                    break              # the click landed; its handler opened a dialog
            w.wait_match(lambda m: True, settle)       # first consequence, or settle elapses

        dialog = None
        with self._wlock:
            pending, self._dialog = self._dialog, None
        if pending is not None:
            try:
                self.cdp("Page.handleJavaScriptDialog", {"accept": self.accept_dialogs},
                         timeout=timeout)
            except HarnessError as error:
                # A page can close its own dialog between javascriptDialogOpening and
                # our dismissal command. Chrome then says "No dialog is showing". The
                # click still happened and the desired terminal state may already exist;
                # turning this harmless race into a navigation failure lost two forms in
                # the 100-job run.
                if not (error.cls is Class.CDP_ERROR
                        and error.observed.get("code") == -32602):
                    raise
                self._j.write("note", event="dialog_already_closed",
                              target_id=self.target_id)
            dialog = {"type": pending.get("type"), "message": pending.get("message")}

        post: list[Any] | None = None
        try:
            post = self._world_js("[location.href, window.__bh ? __bh.mutations : 0]",
                                  timeout=timeout)
        except HarnessError:
            pass                                       # e.g. the click closed the tab
        url_after = post[0] if post else None
        navigated = url_after is not None and url_after != url_before
        mutations = (None if navigated or post is None
                     else max(0, int(post[1]) - mut_before))
        modality = "compositor"
        if (retry_inert and not navigated and not mutations and dialog is None
                and len(self._created) == targets_before):
            landed = self._activate_click(x, y, url_before, mut_before, settle, timeout)
            if landed is not None:
                url_after, mutations, modality = landed
                navigated = url_after is not None and url_after != url_before
        return {
            "url_before": url_before,
            "url_after": url_after,
            "navigated": navigated,
            # a new document restarts the counter, so a cross-document delta would lie
            "dom_mutations": mutations,
            "modality": modality,
            "new_targets": [t.get("targetId") for t in list(self._created)[targets_before:]],
            "dialog": dialog,
        }

    def _activate_click(self, x: float, y: float, url_before: str, mut_before: int,
                        settle: float, timeout: float) -> tuple[Any, int | None, str] | None:
        """Retry a click that provably did nothing, through the DOM.

        A compositor-level click is the right default — it passes through iframes and
        shadow roots that no selector reaches — but it is delivered to a renderer, and the
        renderer can drop it with no error anywhere. Measured on four Recruitee postings:
        clicking Apply navigates and yields 35 fields when the tab is in front, and does
        **nothing at all** for the three of four tabs that are not. `parallel()` puts every
        worker but one in that state, so every click inside a parallel run was silently a
        no-op — which is why Recruitee scored 0/4 in all five historical runs while the
        schema kept reporting ~92 controls waiting behind that button.

        The trigger is the delta, not the tab's visibility. That was the first guess, and
        it was wrong: gating on `document.visibilityState !== 'visible'` still left the
        fixture click failing in a tab that reported itself visible, because occlusion
        tracking is disabled on Windows and the flag therefore says nothing about whether
        the renderer will act. What the harness can actually observe is whether anything
        happened, and it already computes exactly that.

        The condition stays narrow for the obvious reason: it fires only when the click
        was observably inert — no navigation, no DOM mutation, no dialog, no new target.
        A click that did something is never repeated. The residual risk is a control whose
        handler changes only internal JavaScript state, which would then run twice; that is
        accepted deliberately, because the alternative is what was measured — clicks that
        do nothing at all, silently, in every parallel run.

        The dry-run guard is unaffected either way: the danger check already refused submit
        controls before the click, and `SAFETY_JS` blocks form submission in the main world
        regardless of how the click arrived.

        The synthetic path is the FULL gesture — pointerdown, mousedown, focus, pointerup,
        mouseup, click — not a bare `el.click()`, because a bare click() reproduces only
        the last third of what a real click does and both missing thirds are load-bearing:
        combobox widgets open on *mousedown* (the fixture that models SmartRecruiters
        does exactly that), and click() does not move focus, so the keystrokes that follow
        a widget-opening click would land in `document.body` and the typeahead would
        filter on nothing.
        """
        try:
            result = self.js(
                "(() => {"
                f" const x = {x!r}, y = {y!r};"
                " const el = document.elementFromPoint(x, y);"
                " if (!el) return null;"
                " const o = {bubbles: true, cancelable: true, view: window,"
                "            clientX: x, clientY: y, button: 0};"
                " el.dispatchEvent(new PointerEvent('pointerdown',"
                "   {...o, pointerId: 1, isPrimary: true, pointerType: 'mouse'}));"
                " el.dispatchEvent(new MouseEvent('mousedown', o));"
                " try { el.focus && el.focus(); } catch (e) {}"
                " el.dispatchEvent(new PointerEvent('pointerup',"
                "   {...o, pointerId: 1, isPrimary: true, pointerType: 'mouse'}));"
                " el.dispatchEvent(new MouseEvent('mouseup', o));"
                " if (typeof el.click === 'function') el.click();"
                " else el.dispatchEvent(new MouseEvent('click', o));"
                " return true;})()", timeout=timeout)
        except HarnessError:
            return None
        if result is not True:
            # Exactly `true` means "the tab was hidden and I clicked something". Anything
            # else — null, or a value from a page that answered oddly — is not evidence,
            # and acting on a truthy non-answer would activate a control twice.
            return None
        time.sleep(max(settle, 0.15))
        try:
            post = self._world_js("[location.href, window.__bh ? __bh.mutations : 0]",
                                  timeout=timeout)
        except HarnessError:
            return None
        url_after = post[0]
        navigated = url_after != url_before
        return (url_after, None if navigated else max(0, int(post[1]) - mut_before), "dom")

    # -- waiting on a condition, not on a guess ----------------------------

    def wait_for(self, selector: str, *, state: str = "visible", timeout: float = 10.0,
                 settle: float = 0.0) -> dict[str, Any]:
        """Wait for `selector` to be present / visible / gone. Event-driven, never polled.

        The reliability primitive v2 was missing. `wait_lifecycle` answers "the document
        loaded", which an SPA satisfies long before the thing you need exists — so every
        script (mine included: 16 of them across the live checks) fell back to
        `time.sleep(1.2)`, which is simultaneously too slow when the page is fast and wrong
        when the page is slow.

        `state` is `visible` by default rather than `present`: a node that exists but has
        no box is the failure mode that produced a verified write to a 1x1 decoy.
        """
        if state not in ("present", "visible", "gone"):
            raise ValueError(f"state must be present|visible|gone, got {state!r}")
        token = f"w{id(self)}:{time.perf_counter_ns()}"
        with self._j.call("wait_for", selector=selector, state=state):
            # Arm the Python-side waiter BEFORE evaluating: a fast page can satisfy the
            # condition and fire the binding between the evaluate and the wait.
            with self._armed(lambda m: m.get("method") == "Runtime.bindingCalled") as w:
                probe = self._world_js(
                    WATCH_JS.replace("__SEL__", json.dumps(selector))
                            .replace("__STATE__", json.dumps(state))
                            .replace("__TOKEN__", json.dumps(token)),
                    timeout=timeout) or {}
                if not probe.get("matched"):
                    hit = w.wait_match(
                        lambda m: (m.get("params") or {}).get("payload") == token, timeout)
                    if hit is None:
                        self._unwatch(token)
                        raise Timeout(
                            f"{selector!r} was not {state} within {timeout}s",
                            selector=selector, state=state, timeout=timeout)
            if settle:
                time.sleep(settle)
        return {"selector": selector, "state": state,
                "immediate": bool(probe.get("immediate"))}

    def wait_for_form(self, *, min_fields: int = 2, timeout: float = 10.0,
                      settle: float = 0.0) -> dict[str, Any]:
        """Wait until the page holds at least `min_fields` fillable controls.

        Returns `{ready, fields, controls_in_dom, controls_visible, text_len, waited_ms}`
        and **never raises on the not-ready case**, which is the whole point: "there is no
        form on this page" is an ordinary, common answer — roughly a third of live postings
        — not an exception. `wait_for` could only say it by timing out, so every call site
        wrapped it in `try/except HarnessError: pass`, and the 15 seconds it spent getting
        there bought nothing.

        The counts come back either way, so a caller that gets `ready: False` already holds
        the evidence for why, and does not need a second round trip to find out.
        """
        if min_fields < 1:
            raise ValueError(f"min_fields must be >= 1, got {min_fields!r}")
        token = f"f{id(self)}:{time.perf_counter_ns()}"
        t0 = time.perf_counter()
        with self._j.call("wait_for_form", min_fields=min_fields):
            probe, reason = self._watch_document(
                token,
                WATCH_FORM_JS.replace("__MIN__", json.dumps(min_fields))
                             .replace("__TOKEN__", json.dumps(token)),
                timeout=timeout, binding_finishes=True,
            )
            ready = reason in {"terminal", "binding"}
            if ready and settle:
                time.sleep(settle)
            in_dom, visible, text_len = self._world_js(FORM_COUNTS_JS, timeout=timeout) \
                or [0, 0, 0]
        return {"ready": ready, "min_fields": min_fields,
                "immediate": bool(probe.get("immediate")),
                "controls_in_dom": int(in_dom), "controls_visible": int(visible),
                "text_len": int(text_len),
                "waited_ms": round((time.perf_counter() - t0) * 1000, 1)}

    def wait_for_application_state(self, *, timeout: float = 12.0,
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
        token = f"a{id(self)}:{time.perf_counter_ns()}"
        started = time.perf_counter()
        with self._j.call("wait_for_application_state",
                          usable_stable=usable_stable, empty_stable=empty_stable):
            probe, reason = self._watch_document(
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

    def _watch_document(self, token: str, expression: str, *, timeout: float,
                        stable_for: Callable[[dict[str, Any]], float] | None = None,
                        binding_finishes: bool = False) -> tuple[dict[str, Any], str]:
        """Run one observer across document replacements.

        A binding may mean the condition matched (``wait_for_form``) or merely that the
        DOM changed and must be probed again (application state).  A quiet interval is a
        terminal result only when ``stable_for`` supplies the required window.
        """
        deadline = time.monotonic() + timeout
        replaced = {"Runtime.executionContextsCleared", "Page.frameNavigated"}
        consumed: set[int] = set()

        def interesting(message: dict[str, Any]) -> bool:
            return id(message) not in consumed and (
                (message.get("method") == "Runtime.bindingCalled"
                 and (message.get("params") or {}).get("payload") == token)
                or message.get("method") in replaced
            )

        probe: dict[str, Any] = {}
        try:
            with self._armed(lambda message: message.get("method") == "Runtime.bindingCalled"
                             or message.get("method") in replaced) as waiter:
                while (left := deadline - time.monotonic()) > 0:
                    probe = self._world_js(
                        expression, timeout=min(max(left, 0.1), 5.0)) or {}
                    if probe.get("matched"):
                        return probe, "terminal"
                    quiet = stable_for(probe) if stable_for is not None else left
                    hit = waiter.wait_match(interesting, min(quiet, left))
                    if hit is None:
                        return probe, "stable" if stable_for is not None and quiet <= left \
                            else "timeout"
                    consumed.add(id(hit))
                    if binding_finishes and hit.get("method") == "Runtime.bindingCalled":
                        return probe, "binding"
            return probe, "timeout"
        finally:
            self._unwatch(token)

    def _unwatch(self, token: str) -> None:
        """Drop an abandoned observer. A MutationObserver left armed on a busy page runs
        its callback on every DOM change for the life of the document."""
        try:
            self._world_js(
                f"(() => {{const w = window.__bh && __bh.watch;"
                f" if (w && w[{json.dumps(token)}]) {{w[{json.dumps(token)}].disconnect();"
                f" delete w[{json.dumps(token)}];}} return true;}})()", timeout=5.0)
        except HarnessError:
            pass

    def frames(self) -> list[dict[str, Any]]:
        """Cross-origin iframes as attachable targets.

        Same-origin iframes are reachable from `js()` through `contentDocument`; a
        cross-origin one is a separate CDP target and is invisible to every DOM call on the
        parent. Measured live: a SmartRecruiters posting behind DataDome had
        `body.innerText.length === 0` and 10 nodes, with the entire real page inside a
        `geo.captcha-delivery.com` iframe. Without this the page reads as broken rather
        than as bot-walled.

        Attach with `session.tab(target_id)`.
        """
        # Auto-attach is the ONLY way an OOPIF becomes reachable: `Target.getTargets`
        # never lists one (measured: types are page/tab/service_worker/background_page
        # only, even with an explicit filter and --site-per-process), and
        # `attachToTarget` rejects its frame id. Turning it on makes the browser announce
        # each child via `Target.attachedToTarget`, which the registry books.
        # Enabling auto-attach when it is ALREADY on is a no-op, so a second call in the
        # same daemon-backed session announces nothing and the page reads as frameless.
        # Toggling off first forces a full re-announcement, which is why this used to run
        # twice: an optimistic pass, then a retoggling one when the first found nothing.
        # Toggling unconditionally makes the fallback pass unnecessary — and the fallback
        # was not free. It cost a second fixed wait on every frameless page, which is most
        # of them: `prepare_application` measured p50 1225ms / p90 1246 / max 1351 across
        # 160 live calls, a spread far too tight to be work. It was two 0.6s sleeps.
        got: list[dict[str, Any]] = []
        with self._armed(lambda m: m.get("method") == "Target.attachedToTarget") as w:
            self.cdp("Target.setAutoAttach",
                     {"autoAttach": False, "waitForDebuggerOnStart": False,
                      "flatten": True}, timeout=10.0)
            self.cdp("Target.setAutoAttach",
                     {"autoAttach": True, "waitForDebuggerOnStart": False,
                      "flatten": True}, timeout=10.0)
            # Settle rather than sleep: stop as soon as a quiet window passes with no new
            # announcement. This is also a correctness fix. The old `wait_match(lambda m:
            # True, 0.6)` returned on the FIRST announcement and then read `w.hits`
            # immediately, so a page with several OOPIFs reported only the ones that had
            # happened to arrive by then — under-reporting frames, silently.
            deadline = time.monotonic() + FRAMES_MAX_WAIT
            counted = 0
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                w.wait_match(lambda m: False, min(FRAMES_QUIET, left))
                with w.cond:
                    n = len(w.hits)
                if n == counted:
                    break                          # nothing new in a whole quiet window
                counted = n
            for _, msg in w.hits:
                info = (msg.get("params") or {}).get("targetInfo") or {}
                if info.get("type") == "iframe":
                    got.append({"target_id": info["targetId"],
                                "url": info.get("url", ""), "kind": "oopif",
                                "reachable": "session.tab(target_id)"})
        out = got
        # Same-site iframes stay in the parent process and never become targets, so
        # getTargets alone reads as "no iframes" on a page that plainly has one.
        try:
            same = self._world_js(
                "[...document.querySelectorAll('iframe')].map(f => ({src: f.src || '',"
                " same: (() => {try { return !!f.contentDocument; } catch (e) "
                "{ return false; }})()}))", timeout=10.0) or []
        except HarnessError:
            same = []
        if not isinstance(same, list):
            same = []              # a page that answers with anything else has no iframes
        for f in same:
            if isinstance(f, dict) and f.get("same"):
                out.append({"target_id": None, "url": f.get("src", ""),
                            "kind": "same-document", "reachable": "js/contentDocument"})
        return out

    # -- vision: the other half of perception ------------------------------

    def see(self, path: str | Path | None = None, *, marks: bool = True,
            max_dim: int | None = 1400, quality: int = 70,
            timeout: float = 20.0) -> dict[str, Any]:
        """One perception act: the structured elements **and** a screenshot they index.

        v1 ships no extraction helper at all — its SKILL.md hands the agent the
        `getFullAXTree` + `getBoxModel` recipe and says "screenshot when layout or imagery
        matters", so perception is whatever the model writes or sees. v2 shipped one fixed
        extractor instead, which is faster and cheaper (a form schema is ~175 tokens where
        its screenshot is ~3,200) but has exactly one way to be blind, and was: a Select2
        decoy that a schema read as a real field and a human eye would never have typed
        into.

        So neither channel is the default. `see()` returns both, sharing one index: every
        box drawn on the image carries its `ref`, so looking at the picture and acting on
        the DOM are the same decision. `marks=False` gives a clean frame for a human.

        The returned `elements` are the same objects `snapshot()` returns.
        """
        with self._j.call("see", marks=marks):
            els = self._world_js(SNAPSHOT_JS, timeout=timeout) or []
            drawn = 0
            if marks and els:
                drawn = self._world_js(
                    ANNOTATE_JS.replace("__ELS__", json.dumps(els)), timeout=timeout) or 0
            try:
                shot = self.capture_screenshot(path, max_dim=max_dim, quality=quality,
                                               timeout=timeout)
            finally:
                if marks and els:
                    # Always clear the overlay, even if the capture failed — leaving it
                    # would change what every later click lands on.
                    self._world_js(
                        "(() => {const m = document.getElementById('__bh_marks');"
                        " if (m) m.remove(); return true;})()", timeout=timeout)
        return {**shot, "elements": els, "marked": drawn}

    # -- the rest of the promised surface ----------------------------------

    def page_text(self, max_chars: int = 40_000) -> str:
        """Rendered text, truncated. `innerText` not `textContent`: the latter includes
        script bodies and hidden nodes, which is how a "page text" read becomes 200 KB of
        minified JS."""
        with self._j.call("page_text"):
            return self._world_js(
                f"(document.body ? document.body.innerText : '').slice(0, {max_chars})",
                timeout=15.0) or ""

    def press_key(self, key: str, *, modifiers: int = 0,
                  timeout: float = 10.0) -> dict[str, Any]:
        """One named key. `text` is sent only for printable keys — attaching it to Enter or
        Tab makes Chrome insert a character instead of firing the shortcut (v1 paid for
        this with an uncleared field).

        Delivery is verified, not assumed: the renderer drops key events for any tab that
        is not its window's selected tab, and the dispatch ACKs anyway. When the `__bh.keys`
        counter shows nothing arrived, the key is re-dispatched as a DOM event — page
        handlers (Escape closes the popup, ArrowDown moves the highlight) still run;
        browser default actions (Tab focus move, Enter submit) do not, and Enter's submit
        is refused by the dry-run guard regardless. The returned `modality` says which
        path delivered.
        """
        spec = _KEYS.get(key)
        code, text = (spec if spec else (key, key if len(key) == 1 else ""))
        if key == "Enter":
            danger = self._world_js(
                f"(() => {{const el = document.activeElement;"
                " if (!el || el.tagName === 'TEXTAREA' || el.isContentEditable) return {danger:false};"
                f" const direct = ({_DANGER_JS})(el); if (direct && direct.danger) return direct;"
                " const form = el.form || (el.closest && el.closest('form'));"
                " return {danger: !!form, tag: el.tagName.toLowerCase(), type: el.type || null,"
                " action: form && form.action || ''};})()", timeout=timeout)
            self._refuse_danger(danger, key=key)
        base: dict[str, Any] = {"key": key, "code": code, "modifiers": modifiers}
        down = {**base, "type": "keyDown"}
        if text and not modifiers:
            down["text"] = text
        with self._j.call("press_key", key=key):
            pre = _count(self._world_js("window.__bh ? __bh.keys : 0", timeout=timeout))
            self.cdp("Input.dispatchKeyEvent", down, timeout=timeout)
            self.cdp("Input.dispatchKeyEvent", {**base, "type": "keyUp"}, timeout=timeout)
            result = self._world_js(
                _SYNTH_KEY_JS.replace("__PRE__", json.dumps(pre))
                             .replace("__KEY__", json.dumps(key))
                             .replace("__CODE__", json.dumps(code))
                             .replace("__TEXT__", json.dumps(text))
                             .replace("__MODS__", json.dumps(modifiers)),
                timeout=timeout)
            if not isinstance(result, dict):
                result = {}            # a non-dict answer is not evidence of a drop
            if result.get("synthesized"):
                self._j.write("note", event="input_synthesized", input="key", key=key,
                              target_id=self.target_id)
        return {"key": key,
                "modality": "dom" if result.get("synthesized") else "compositor"}

    def type_chars(self, text: str, *, ref: str | None = None, timeout: float = 10.0,
                   settle: float = 0.0) -> dict[str, Any]:
        """Per-character trusted key events, VERIFIED delivered, DOM-synthesized when not.

        The verification exists because both facts below are measured, and together they
        made every typed write inside `parallel()` a silent no-op:

          - only per-character `dispatchKeyEvent` drives a keystroke typeahead (a one-shot
            value write opened it 0 times, `Input.insertText` 0 times, real key events 5);
          - the renderer drops those very events for any tab that is not its window's
            selected tab — 0 of 2 keydowns seen by page listeners, no error anywhere —
            and `parallel()` puts every worker but at most one in exactly that state.

        `ref` names the intended field so the fallback cannot type into whatever happens
        to hold focus; without it the synthetic path uses `document.activeElement`, which
        is where the trusted events would have gone too.
        """
        text = str(text)
        pre = _count(self._world_js("window.__bh ? __bh.keys : 0", timeout=timeout))
        for ch in text:
            # keyDown carrying `text` is what makes the page see a real character; the
            # matching keyUp is what a keystroke-driven typeahead listens for.
            self.cdp("Input.dispatchKeyEvent", {"type": "keyDown", "text": ch,
                                                "key": ch, "unmodifiedText": ch},
                     timeout=timeout)
            self.cdp("Input.dispatchKeyEvent", {"type": "keyUp", "key": ch},
                     timeout=timeout)
        if settle:
            time.sleep(settle)
        result = self._world_js(
            _SYNTH_KEYS_JS.replace("__PRE__", json.dumps(pre))
                          .replace("__TEXT__", json.dumps(text))
                          .replace("__REF__", json.dumps(ref)),
            timeout=timeout)
        if not isinstance(result, dict):
            result = {}                # a non-dict answer is not evidence of a drop
        if result.get("synthesized"):
            self._j.write("note", event="input_synthesized", input="keys",
                          chars=len(text), target_id=self.target_id)
            if settle:
                time.sleep(settle)     # the widget filters on those events; let it
        out = {"chars": len(text),
               "modality": "dom" if result.get("synthesized") else "compositor",
               "delivered": int(result.get("delivered") or 0)}
        if result.get("error"):
            out["error"] = result["error"]
        return out

    def scroll(self, dy: int = 600, dx: int = 0, *, x: int = 400, y: int = 300,
               timeout: float = 10.0) -> dict[str, Any]:
        """Wheel event at a point, so it scrolls whatever container is under the cursor —
        an overflow pane, a virtualised list — not just the document.

        Verified like every other raw input: if no scroll event reached the document (the
        renderer drops wheel events for non-selected tabs), the same container is scrolled
        through the DOM instead, and `modality` reports which path ran.
        """
        with self._j.call("scroll", dy=dy, dx=dx):
            pre = _count(self._world_js("window.__bh ? __bh.scrolls : 0", timeout=timeout))
            try:
                # Short leash, deliberately: a hidden renderer does not merely drop a
                # wheel event the way it drops keys and clicks — it never ACKs the
                # dispatch at all (measured: 10s CDP timeout in a background tab). The
                # timeout IS the non-delivery signal, so it is caught and the verified
                # fallback below takes over, exactly as the dialog dance treats a click
                # dispatch that cannot ACK.
                self.cdp("Input.dispatchMouseEvent",
                         {"type": "mouseWheel", "x": x, "y": y,
                          "deltaX": dx, "deltaY": dy},
                         timeout=min(timeout, 2.0))
            except Timeout:
                pass
            time.sleep(0.15)           # scroll events fire after the compositor applies
            result = self._world_js(
                _SYNTH_SCROLL_JS.replace("__PRE__", json.dumps(pre))
                                .replace("__X__", json.dumps(x))
                                .replace("__Y__", json.dumps(y))
                                .replace("__DX__", json.dumps(dx))
                                .replace("__DY__", json.dumps(dy)),
                timeout=timeout)
            if not isinstance(result, dict):
                result = {}
            if result.get("modality") == "dom":
                self._j.write("note", event="input_synthesized", input="scroll",
                              dy=dy, dx=dx, target_id=self.target_id)
        return result

    def upload_file(self, ref: str, paths: str | list[str], *,
                    timeout: float = 20.0) -> MappingOutcome:
        """Set a file input's files without touching the OS picker.

        `ref` is a snapshot ref **or** a CSS selector — hidden file inputs never reach the
        snapshot registry, so a selector is usually the only way to name one.

        `DOM.setFileInputFiles` needs a backendNodeId, and the ref registry holds a JS
        handle — so the bridge is `Runtime.evaluate(returnByValue=false)` to get an object
        id, then `DOM.describeNode`. Clicking the input instead would open a native dialog
        that blocks the renderer with no CDP way back out.

        The mapping return also exposes the standard Outcome attributes. It says what
        actually happened, because `attached: []` alone cannot
        distinguish three different outcomes: a page whose change handler consumed the
        file and cleared the input (success), a file the input's `accept` filtered out,
        and a ref that was never a file input. The second and third are now loud —
        pointing this at the wrong element used to look exactly like success.
        """
        if os.environ.get("BH_DISABLE_FILE_UPLOADS", "").strip().lower() in {
            "1", "true", "yes",
        }:
            raise SideEffectRefused(
                "file upload disabled by BH_DISABLE_FILE_UPLOADS", ref=ref)
        files = [paths] if isinstance(paths, str) else list(paths)
        missing = [f for f in files if not Path(f).is_file()]
        if missing:
            raise ElementGone(f"no such file(s): {missing}", files=missing)
        resolve = _resolve_js(ref)
        ctx = self._ensure_world()
        params: dict[str, Any] = {"expression": resolve, "returnByValue": False}
        if ctx is not None:
            params["contextId"] = ctx
        handle = self.cdp("Runtime.evaluate", params, timeout=timeout).get("result") or {}
        if not handle.get("objectId"):
            raise ElementGone(
                f"no element for {ref!r} — not a registered ref, and no element matches it "
                f"as a CSS selector", ref=ref)
        el = self._world_js(
            f"(() => {{const e = {resolve}; if (!e) return null;"
            " return {tag: e.tagName.toLowerCase(), type: e.type || null,"
            "  name: e.name || e.id || null, accept: e.accept || ''};})()",
            timeout=timeout) or {}
        if el.get("tag") != "input" or el.get("type") != "file":
            # The failure that motivated this check: snapshot() skipped a display:none
            # CV input, so the only file ref on the page was an unrelated 1x1 control,
            # and setting files on it reported `attached: []` — indistinguishable from
            # the success case. Refuse instead of guessing.
            raise ElementGone(
                f"ref {ref!r} is <{el.get('tag')} type={el.get('type')!r}>, not a file "
                f"input — setting files on it would silently do nothing",
                ref=ref, tag=el.get("tag"), type=el.get("type"))
        node = self.cdp("DOM.describeNode", {"objectId": handle["objectId"]},
                        timeout=timeout)["node"]
        with self._j.call("upload_file", ref=ref, n=len(files)):
            self.cdp("DOM.setFileInputFiles",
                     {"files": [str(Path(f).resolve()) for f in files],
                      "backendNodeId": node["backendNodeId"]}, timeout=timeout)
        got = self._world_js(
            f"(() => {{const e = {resolve}; if (!e) return [];"
            f" return [...(e.files||[])].map(f => f.name);}})()", timeout=timeout)
        out: dict[str, Any] = {"ref": ref, "attached": got or [], "requested": len(files),
                               "accept": el.get("accept") or ""}
        attached = set(got or [])
        rejected = [f for f in files if Path(f).name not in attached
                    and not _accepts(el.get("accept") or "", f)]
        if not got:
            # Empty is normal when the page's change handler moves the file into its own
            # state and clears the input. It is NOT normal when `accept` excluded the file
            # — that one is a silent client-side rejection, so name it.
            out["consumed_or_rejected"] = True
        if rejected:
            out["accept_rejected"] = [Path(f).name for f in rejected]
            return MappingOutcome(Outcome(
                ok=False,
                cls=Class.VALUE_REJECTED,
                detail="file input rejected file(s) excluded by its accept filter",
                observed=out,
                value=out,
            ))
        return MappingOutcome(ok(out, **out))

    # -- item 21: screenshots ----------------------------------------------

    def capture_screenshot(self, path: str | Path | None = None, *, quality: int = 70,
                           max_dim: int | None = None, timeout: float = 20.0) -> dict[str, Any]:
        """JPEG by default (PNG when the path says so); output pixels == CSS viewport
        pixels on any display: `clip.scale = 1/devicePixelRatio` (item 21). `max_dim`
        lowers the scale further instead of resizing afterwards."""
        fmt = "png" if str(path or "").endswith(".png") else "jpeg"
        with self._j.call("screenshot", format=fmt):
            m = self.cdp("Page.getLayoutMetrics", timeout=timeout)
            css = m.get("cssLayoutViewport") or m["layoutViewport"]
            cw, ch = css["clientWidth"], css["clientHeight"]
            dpr = float(self.js("devicePixelRatio", timeout=5.0) or 1)
            scale = 1.0 / dpr
            if max_dim:
                scale = min(scale, max_dim / (max(cw, ch) * dpr))
            params: dict[str, Any] = {"format": fmt, "clip": {
                "x": css.get("pageX", 0), "y": css.get("pageY", 0),
                "width": cw, "height": ch, "scale": scale}}
            if fmt == "jpeg":
                params["quality"] = quality
            data = base64.b64decode(self.cdp("Page.captureScreenshot", params,
                                             timeout=timeout)["data"])
        if path:
            Path(path).write_bytes(data)
        return {"path": str(path) if path else None, "bytes": len(data), "format": fmt,
                "css_viewport": [cw, ch], "scale": scale}
