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
import math
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
from harness.core.content import ContentStore
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
from harness.ops.semantic import SemanticPageCache

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
#: many times the gap it has to bridge.
FRAMES_QUIET = 0.12
#: A trustworthy zero host probe is only a snapshot. Give an SPA this short window to
#: materialise an OOPIF after the probe, with `Target.attachedToTarget` as the wakeup.
#: Truly frameless pages pay 200ms, not the historical pair of 600ms fixed sleeps.
FRAMES_ZERO_OBSERVE = 0.2
#: Ceiling for the settle loop, so a page that keeps spawning iframes still terminates.
FRAMES_MAX_WAIT = 0.8

#: The automatic usable-document grace is deliberately session-local and bounded.  Two
#: exact navigations are enough to shorten the cold 3 s ceiling, but one unusually fast
#: page is not.  The rolling maximum is conservative across a mixed session; the margin
#: absorbs event/evaluation scheduling noise.  An explicit smaller ``usable_after`` remains
#: a smaller caller-owned ceiling, while ``None`` disables every early fallback.
NAVIGATION_GRACE_MIN = 0.5
NAVIGATION_GRACE_MAX = 3.0
NAVIGATION_GRACE_MARGIN = 0.25
NAVIGATION_HISTORY_MIN = 2
NAVIGATION_HISTORY_MAX = 8

#: A fallback is useful only when the observed document and content-producing network are
#: both quiet.  Two equal bounded probes across this interval are the strict mechanical
#: invariant that prevents an early SPA shell from being promoted merely because it has a
#: heading.  XHR/fetch/event-stream requests additionally block while they are in flight.
NAVIGATION_QUIET = 0.15
NAVIGATION_STABLE = 0.15
#: How long to wait before asking a quiet, not-yet-usable document again, and the ceiling
#: that backoff climbs to. A document that is still empty at the grace mark is not
#: evidence that it will stay empty — but the only thing that used to trigger a second
#: look was new network activity, so a page rendering from script it had already fetched
#: got exactly one chance. Measured on the 2026-08-26 corpus: lowering the grace from 3.0s
#: to 0.8s turned four such pages into `no 'load' lifecycle event` timeouts, because the
#: single probe moved earlier rather than repeating.
NAVIGATION_REPROBE = 0.15
NAVIGATION_REPROBE_MAX = 2.0
NAVIGATION_DATA_TYPES = frozenset({"XHR", "Fetch", "EventSource"})
NAVIGATION_EVENTS = frozenset({
    "Page.lifecycleEvent",
    "Network.requestWillBeSent",
    "Network.loadingFinished",
    "Network.loadingFailed",
})

#: Network observations retained for automatic public JSON batching. Both stores are
#: bounded independently: a page can leave requests hanging forever, so bounding only the
#: completed deque would still let the pending table grow without limit. Values retain
#: URLs and boolean credential evidence, never headers or response bodies.
ENDPOINT_OBSERVATION_LIMIT = 256
ENDPOINT_PENDING_LIMIT = 512
ENDPOINT_URL_LIMIT = 4096

#: Installed on every new document (item 18). Idempotent; `__bh.mutations` is the DOM
#: delta counter, `__bh.refs` the snapshot ref registry. Lives in the isolated world, so
#: `__bh` is reachable from harness JS and invisible to the page.
RUNTIME_JS = """(() => {
  const bh = window.__bh || (window.__bh = {refs: {}, n: 0, mutations: 0});
  if (bh.runtime) return;
  bh.runtime = true;
  bh.changes = bh.changes || [];
  bh.changeFloor = bh.changeFloor || 0;
  bh.actionStarts = bh.actionStarts || {};
  bh.beginAction = token => {
    const keys = Object.keys(bh.actionStarts);
    if (keys.length >= 128) delete bh.actionStarts[keys[0]];
    bh.actionStarts[token] = bh.mutations || 0;
    return bh.actionStarts[token];
  };
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
  const obs = new MutationObserver(list => {
    bh.mutations += list.length;
    const remember = (node, removed = false) => {
      if (!node) return;
      const element = node.nodeType === 1 ? node : node.parentElement;
      if (!element) return;
      bh.changes.push({n: bh.mutations, node: element, removed});
      if (bh.changes.length > 128) {
        const dropped = bh.changes.splice(0, bh.changes.length - 128);
        for (const change of dropped)
          bh.changeFloor = Math.max(bh.changeFloor, Number(change.n || 0));
      }
    };
    for (const mutation of list) {
      // The harness's own dry-run marker is safety bookkeeping, not a page result.
      if (mutation.type === 'attributes'
          && mutation.attributeName === 'data-bh-entered') continue;
      const added = [...(mutation.addedNodes || [])];
      const removed = [...(mutation.removedNodes || [])];
      // For child-list changes, the changed children are the bounded semantic evidence.
      // Recording their large parent as well turns a one-line insertion into a 4 KiB
      // body dump and obscures which region actually changed.
      if (mutation.type !== 'childList' || (!added.length && !removed.length))
        remember(mutation.target);
      for (const node of added) remember(node);
      for (const node of removed) remember(node, true);
    }
  });
  const arm = () => obs.observe(document.documentElement || document,
    {subtree: true, childList: true, attributes: true, characterData: true});
  document.documentElement ? arm() : document.addEventListener('DOMContentLoaded', arm);
  // Delivery counters, same idea as `mutations`: raw Input.* events are handed to a
  // renderer that may never act on them, and the CDP call ACKs either way. Measured with
  // page-side listeners on a hidden tab: dispatchKeyEvent x2 -> 0 keydowns seen,
  // dispatchMouseEvent -> 0 clicks; the same tab after Target.activateTarget -> all
  // arrive. That drop is CONDITIONAL — the same Chrome build driven through a visible,
  // long-lived window delivers both to a `document.hidden` tab — so the exact trigger is
  // not tab selection alone and is not settled (see D1's qualification). Which is why
  // nothing here depends on knowing it: the counters measure delivery directly.
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

_CONTROL_STATE_JS = """(el => {
  if (!el) return null;
  const label = el.closest && el.closest('label');
  if (label && label.control) el = label.control;
  const summary = el.closest && el.closest('summary');
  if (summary && summary.parentElement && summary.parentElement.tagName === 'DETAILS')
    el = summary.parentElement;
  const control = el.closest && el.closest('input,option,select,details,button,[role=button]');
  if (!control) return null;
  const tag = control.tagName.toLowerCase();
  const type = String(control.type || '').toLowerCase();
  const state = {tag, type: type || null};
  if (tag === 'input' && (type === 'checkbox' || type === 'radio'))
    state.checked = !!control.checked;
  if (tag === 'option') state.selected = !!control.selected;
  if (tag === 'select') state.selectedIndex = control.selectedIndex;
  if (tag === 'details') state.open = !!control.open;
  if ('value' in control) state.value = type === 'password'
    ? (control.value ? '[set]' : '') : String(control.value || '').slice(0, 1000);
  if (control.validity) {
    state.valid = !!control.validity.valid;
    state.valueMissing = !!control.validity.valueMissing;
    state.typeMismatch = !!control.validity.typeMismatch;
    state.patternMismatch = !!control.validity.patternMismatch;
    state.validationMessage = String(control.validationMessage || '').slice(0, 500);
  }
  for (const name of [
    'aria-checked', 'aria-expanded', 'aria-pressed', 'aria-selected', 'aria-invalid'
  ]) {
    if (control.hasAttribute && control.hasAttribute(name)) state[name] = control.getAttribute(name);
  }
  return state;
})"""

_ACTION_CONSEQUENCE_JS = r"""((marker, refs) => {
  const bh = window.__bh || {mutations: 0, changes: [], actionStarts: {}, refs: {}};
  let before = marker;
  if (typeof marker === 'string') {
    before = bh.actionStarts[marker];
    try { delete bh.actionStarts[marker]; } catch (e) {}
  }
  before = Number(before || 0);
  const targets = refs.map(ref => bh.refs[ref]).filter(Boolean);
  const semantic = node => {
    if (!node || !node.closest) return null;
    return node.closest('[role=dialog],[role=alert],[aria-live],dialog,'
      + 'p,li,table,fieldset,section,article,form') || node;
  };
  const seen = new Set(), regions = [];
  let chars = 0, truncated = before < Number(bh.changeFloor || 0);
  let modal = false, related = 0;
  for (const change of bh.changes || []) {
    if (Number(change.n || 0) <= before) continue;
    const region = semantic(change.node);
    if (!region || seen.has(region)) continue;
    seen.add(region);
    const role = String(region.getAttribute && region.getAttribute('role') || '').toLowerCase();
    const tag = String(region.tagName || '').toLowerCase();
    const isModal = role === 'dialog' || role === 'alert' || tag === 'dialog';
    const isRelated = targets.some(target => region === target || region.contains(target)
      || target.contains(region) || (target.form && region === target.form)
      || (region.id && String(target.getAttribute && target.getAttribute('aria-controls') || '')
        .split(/\s+/).includes(region.id)));
    let text = String(region.innerText || region.textContent || '')
      .replace(/\s+/g, ' ').trim();
    const room = Math.max(0, 4000 - chars);
    if (text.length > room) { text = text.slice(0, room); truncated = true; }
    const row = {kind: isModal ? 'modal' : role || tag || 'region', text,
      related: isRelated, removed: !!change.removed};
    if (region.id) row.ref = `#${String(region.id).slice(0, 120)}`;
    regions.push(row); chars += text.length;
    modal = modal || isModal; related += isRelated ? 1 : 0;
    if (regions.length >= 12 || chars >= 4000) { truncated = true; break; }
  }
  const states = {};
  for (const ref of refs) {
    const el = bh.refs[ref];
    states[ref] = el ? (__CONTROL_STATE__)(el) : null;
  }
  return {mutation_count: Math.max(0, Number(bh.mutations || 0) - before),
    changed_regions: regions, regions_truncated: truncated, modal,
    history_truncated: before < Number(bh.changeFloor || 0),
    related_regions: related, states};
})(__MARKER__, __REFS__)""".replace("__CONTROL_STATE__", _CONTROL_STATE_JS)

#: Native/property-only changes are direct evidence that a compositor click landed even
#: when MutationObserver saw nothing. Focus is deliberately absent: a press can focus a
#: button without its activation handler running, and treating that as success suppresses
#: the background-tab DOM fallback while the requested action still did nothing.
_CONTROL_DELTA_KEYS = frozenset({
    "checked", "selected", "selectedIndex", "open",
    "aria-checked", "aria-expanded", "aria-pressed", "aria-selected",
})

_VALIDATION_DELTA_KEYS = frozenset({
    "value", "checked", "selected", "selectedIndex", "valid", "valueMissing",
    "typeMismatch", "patternMismatch", "validationMessage", "aria-invalid",
})


_SEMANTIC_DIGEST_JS = r"""(() => {
  const maxChars = __MAX_CHARS__, maxLinks = __MAX_LINKS__, start = __START__;
  const maxBlocks = 500, maxSemanticChars = 400000, maxBlockChars = 100000;
  const contentOnly = __CONTENT_ONLY__;
  const root = contentOnly
    ? (document.querySelector('main,[role=main],article') || document.body)
    : document.body;
  const clean = value => String(value || '').replace(/\r/g, '')
    .replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  const visible = el => {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return !!(r.width && r.height) && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const path = el => {
    if (el.id) return `${el.tagName.toLowerCase()}#${String(el.id).slice(0, 160)}`;
    const parts = [];
    for (let node = el; node && node.nodeType === 1 && node !== root; node = node.parentElement) {
      const tag = node.tagName.toLowerCase();
      let index = 1;
      for (let sibling = node.previousElementSibling; sibling; sibling = sibling.previousElementSibling)
        if (sibling.tagName === node.tagName) index++;
      parts.push(`${tag}:nth-of-type(${index})`);
      if (parts.length >= 8) break;
    }
    return parts.reverse().join('>') || (el.tagName || 'region').toLowerCase();
  };
  const rows = [];
  let semanticChars = 0, blockCandidates = 0, blocksTruncated = false;
  const add = (el, kind, text, extra = {}) => {
    const normalized = clean(text);
    if (!normalized && !extra.state && !extra.links) return;
    blockCandidates++;
    if (rows.length >= maxBlocks || semanticChars >= maxSemanticChars) {
      blocksTruncated = true; return;
    }
    const room = Math.min(maxBlockChars, maxSemanticChars - semanticChars);
    const bounded = normalized.slice(0, room);
    semanticChars += bounded.length;
    const value = {kind, key: `${kind}:${path(el)}`, text: bounded, ...extra};
    if (bounded.length < normalized.length) {
      value.text_chars = normalized.length; value.text_truncated = true;
      blocksTruncated = true;
    }
    rows.push({node: el, value});
  };
  if (root) {
    const semantic = root.querySelectorAll(
      'h1,h2,h3,h4,h5,h6,p,pre,blockquote,ul,ol,dl,table');
    for (const el of semantic) {
      if (!visible(el)) continue;
      if (el.matches('p,pre,blockquote') && el.closest('li,table')) continue;
      let kind = 'paragraph', extra = {};
      if (/^H[1-6]$/.test(el.tagName)) {
        kind = 'heading'; extra = {level: Number(el.tagName.slice(1))};
      } else if (el.matches('ul,ol,dl')) kind = 'list';
      else if (el.matches('table')) kind = 'table';
      else if (el.matches('pre')) kind = 'preformatted';
      else if (el.matches('blockquote')) kind = 'quote';
      add(el, kind, el.innerText || el.textContent, extra);
    }

    const controls = root.querySelectorAll(
      'input:not([type=hidden]),textarea,select,button,[role=button],'
      + '[role=checkbox],[role=radio],[role=combobox]');
    for (const el of controls) {
      if (!visible(el)) continue;
      const tag = el.tagName.toLowerCase(), type = String(el.type || '').toLowerCase();
      const labelled = el.labels && el.labels.length ? [...el.labels]
        .map(label => label.innerText || label.textContent).join(' ') :
        (el.getAttribute('aria-label') || el.getAttribute('placeholder')
          || (tag === 'button' || el.getAttribute('role') === 'button'
            ? (el.innerText || el.textContent) : '') || el.name || el.id);
      const state = {tag, type: type || null, required: !!el.required,
        disabled: !!el.disabled, aria_invalid: el.getAttribute('aria-invalid')};
      if ('checked' in el) state.checked = !!el.checked;
      if (tag === 'select') state.selected = [...el.selectedOptions]
        .map(option => clean(option.textContent)).filter(Boolean).slice(0, 20);
      else if (type === 'file') state.files = el.files ? el.files.length : 0;
      else if ('value' in el) state.value = type === 'password'
        ? (el.value ? '[set]' : '') : String(el.value || '').slice(0, 1000);
      if (el.validity) {
        state.valid = !!el.validity.valid;
        state.value_missing = !!el.validity.valueMissing;
        state.type_mismatch = !!el.validity.typeMismatch;
        state.pattern_mismatch = !!el.validity.patternMismatch;
        state.validation_message = String(el.validationMessage || '').slice(0, 500);
      }
      add(el, 'control', labelled || el.innerText || el.value, {control: {tag, type}, state});
    }

    // Modern SPAs commonly render prose in generic div/section containers. Preserve
    // their direct text without duplicating nested headings, paragraphs, lists, controls,
    // links, or child regions, so one changed leaf still maps to one stable block.
    const genericSelector =
      'main,section,article,div,form,fieldset,[role=main],[role=region],[role=article]';
    const ownedSelector = 'h1,h2,h3,h4,h5,h6,p,pre,blockquote,ul,ol,dl,table,'
      + genericSelector + ',input,textarea,select,button,[role=button],'
      + '[role=checkbox],[role=radio],[role=combobox],a,label';
    const generic = [root, ...root.querySelectorAll(genericSelector)];
    for (const el of generic) {
      if (!visible(el)) continue;
      const direct = [];
      for (const node of el.childNodes) {
        if (node.nodeType === Node.TEXT_NODE) direct.push(node.textContent || '');
        else if (node.nodeType === Node.ELEMENT_NODE
                 && !node.matches(ownedSelector) && !node.querySelector(ownedSelector))
          direct.push(node.innerText || node.textContent || '');
      }
      add(el, 'region', direct.join(' '));
    }
  }

  const links = [], seen = new Set(), linkNodes = [];
  let linkCandidates = 0, linksTruncated = false;
  const scopes = root && root !== document.body ? [root, document] : [document];
  for (const scope of scopes) {
    for (const a of scope.querySelectorAll('a[href]')) {
      if (links.length >= maxLinks) { linksTruncated = true; break; }
      if (scope === document && root && root.contains(a)) continue;
      if (contentOnly && a.closest('nav,header,footer,aside')) continue;
      if (!visible(a)) continue;
      let href;
      try { href = new URL(a.href, location.href).href; } catch { continue; }
      linkCandidates++;
      if (href.length > 4096) { linksTruncated = true; continue; }
      if (!/^https?:/i.test(href) || seen.has(href)) continue;
      seen.add(href);
      const text = clean(a.innerText || a.getAttribute('aria-label')).slice(0, 160);
      links.push({text, href}); linkNodes.push([a, {text, href}]);
    }
    if (links.length >= maxLinks) break;
  }
  const groups = new Map();
  for (const [anchor, link] of linkNodes) {
    const owner = anchor.closest('nav,[role=navigation]') || anchor.parentElement;
    if (!owner || (root && !root.contains(owner))) continue;
    if (!groups.has(owner)) groups.set(owner, []);
    groups.get(owner).push(link);
  }
  for (const [owner, grouped] of groups)
    if (grouped.length >= 2) add(owner, 'link_group',
      grouped.map(link => link.text).filter(Boolean).join('\n'), {links: grouped});

  rows.sort((a, b) => {
    if (a.node === b.node) return a.value.kind.localeCompare(b.value.kind);
    const relation = a.node.compareDocumentPosition(b.node);
    return relation & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
  });

  let raw = clean((root && root.innerText) || '');
  if (contentOnly && root) {
    for (const select of root.querySelectorAll('select')) {
      const verbose = clean(select.innerText || select.textContent);
      const chosen = [...select.selectedOptions].map(option => clean(option.textContent))
        .filter(Boolean);
      if (verbose && chosen.length) raw = raw.replace(verbose, `[selected: ${chosen.join(', ')}]`);
    }
  }
  // SPAs often render a useful result as direct text inside <main> rather than a <p>.
  // Do not drop that document merely because it has no more specific semantic element.
  if (!rows.length && root && raw)
    rows.push({node: root, value: {kind: 'region', key: `region:${path(root)}`, text: raw}});
  const signals = [];
  const challengeSelector = [
    'iframe[src*="captcha" i]', 'iframe[src*="challenge" i]',
    '[id*="captcha" i]', '[class*="captcha" i]', '[data-sitekey]'
  ].join(',');
  if (document.querySelector(challengeSelector)) signals.push('challenge_dom');
  const assets = [...document.querySelectorAll('iframe[src],script[src]')]
    .map(el => String(el.getAttribute('src') || '')).join(' ').toLowerCase();
  if (/(?:recaptcha|hcaptcha|captcha-delivery|challenge-platform|turnstile)/.test(assets))
    signals.push('challenge_asset');
  const lead = (String(document.title || '') + '\n' +
    String((document.body && document.body.innerText) || '').slice(0, 6000)).toLowerCase();
  const humanPattern = /verify (?:that )?you are human|complete (?:the )?security check|are you (?:a )?robot|unusual traffic|press and hold|human verification/;
  if (humanPattern.test(lead)) signals.push('human_verification_text');
  const uniqueSignals = [...new Set(signals)];
  return {
    document_id: String(performance.timeOrigin || 0),
    url: location.href, title: document.title, ready_state: document.readyState,
    language: document.documentElement.lang || navigator.language || '',
    // Retained for backwards-compatible raw-character paging. New continuations use
    // the document-bound semantic cursor returned by Python.
    text: raw.slice(start, start + maxChars), text_chars: raw.length,
    text_start: start, text_remaining: Math.max(0, raw.length - start - maxChars),
    text_truncated: raw.length > start + maxChars,
    blocks: rows.map(row => row.value), block_chars: semanticChars,
    block_candidates: blockCandidates, blocks_truncated: blocksTruncated,
    links, link_candidates: linkCandidates, links_truncated: linksTruncated,
    challenge: {detected: uniqueSignals.length > 0,
      confidence: uniqueSignals.includes('challenge_asset') ||
                  uniqueSignals.includes('challenge_dom') ? 'high' :
                  uniqueSignals.length ? 'medium' : 'none',
      signals: uniqueSignals}
  };
})()"""


def _control_state_changed(before: Any, after: Any) -> bool:
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    return any(
        (key in before, before.get(key)) != (key in after, after.get(key))
        for key in _CONTROL_DELTA_KEYS
    )


def _validation_delta(before: Any, after: Any) -> dict[str, Any]:
    prior = before if isinstance(before, dict) else {}
    current = after if isinstance(after, dict) else {}
    keys = [key for key in _VALIDATION_DELTA_KEYS
            if (key in prior, prior.get(key)) != (key in current, current.get(key))]
    return {"changed": bool(keys), "keys": sorted(keys),
            "before": {key: prior.get(key) for key in keys},
            "after": {key: current.get(key) for key in keys}}

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
  // `_watch_document` re-evaluates this on every wakeup. Without this the previous
  // iteration's observer was overwritten in the map but never disconnected, so a page
  // that woke the wait five times was left running five MutationObservers, four of them
  // unreachable — and a `matched` return skipped the arming code that stores the fifth.
  bh.watch = bh.watch || {};
  if (bh.watch[token]) { bh.watch[token].disconnect(); delete bh.watch[token]; }
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
#: A CSS query for every element that can host a child browsing context. frames() passes
#: it to DOM.performSearch rather than querySelectorAll: CDP search pierces *closed* shadow
#: roots, while page JavaScript sees `host.shadowRoot === null` there. Measured 2026-08-21
#: against real Chrome: a frameless document returned zero, while plain and closed-shadow
#: cross-site iframes each returned one. The broader host selector is conservative: a
#: frame/object/embed false positive merely pays the attach dance. Page.getFrameTree is not
#: a substitute: it reports in-process children and omits the OOPIF this gate discovers.
FRAME_HOST_QUERY = "iframe,frame,object,embed"

WATCH_APPLICATION_STATE_JS = """((token) => {
  const bh = window.__bh;
  // Before the read, not after: `_watch_document` re-evaluates this on every wakeup, and
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

    def wait_next(self, cursor: int, timeout: float) -> tuple[
            int, tuple[float, dict[str, Any]] | None]:
        """Return one buffered event exactly once, waiting event-first up to ``timeout``.

        Navigation needs to update factual in-flight request state for *non-terminal*
        events.  ``wait_match`` deliberately hides those; a cursor keeps this path
        event-driven without repeatedly applying state transitions to the same message.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self.cond:
            while cursor >= len(self.hits):
                left = deadline - time.monotonic()
                if left <= 0:
                    return cursor, None
                self.cond.wait(left)
            return cursor + 1, self.hits[cursor]


class Tab:
    """Primitives bound to one target. All CDP goes through the target's registered
    session; all waits go through one subscriber registered at construction."""

    def __init__(self, conn: Connection, registry: SessionRegistry, target_id: str, *,
                 journal: Journal | None = None, accept_dialogs: bool = False,
                 content_store: ContentStore | None = None):
        self._conn, self._reg, self.target_id = conn, registry, target_id
        self._j = journal or conn.journal
        self._content_store = content_store or ContentStore()
        self._semantic = SemanticPageCache(self._content_store, self._j, target_id)
        self.accept_dialogs = accept_dialogs
        self._session_id: str | None = None
        self._wlock = threading.Lock()
        self._waiters: list[_Waiter] = []
        self._dialog: dict[str, Any] | None = None
        #: (monotonic ts, params) of the last dialog the auto-resolver dismissed — how a
        #: click whose dispatch was blocked learns its dialog was already handled.
        self._auto_dialog: tuple[float, dict[str, Any]] | None = None
        #: (sequence number, targetInfo) for every page target THIS tab opened. Bounded,
        #: because a page that opens popups in a loop must not grow it without limit — and
        #: therefore paired with a counter, because a bounded deque's length stops changing
        #: once it is full and so cannot serve as the position a click reads its delta
        #: from. Written only by the reader thread (see `_on_event`).
        self._created: deque[tuple[int, dict[str, Any]]] = deque(maxlen=16)
        self._created_seq = 0
        self._diagnostic_events: deque[dict[str, Any]] = deque(maxlen=128)
        self._diagnostics_enabled = False
        self._diagnostics_started = 0.0
        self._world_ctx: int | None = None
        #: Worlds we have already tried to repopulate. One heal per world: enough to fix a
        #: world the document-start registration missed, bounded so a genuine bug in this
        #: module's JS cannot re-run on every call.
        self._healed_worlds: set[int] = set()
        #: Main-frame id, learned from `Page.frameNavigated` and kept for the life of the
        #: Tab. A frame belongs to the target, not to the session (a session is only a
        #: lease), and Chrome keeps the main frame's id stable across its navigations — so
        #: this survives both a document replacement and a session recovery, and
        #: `Page.getFrameTree` is only ever paid before the first navigation is seen.
        self._main_frame: str | None = None
        #: Empty `DOM.performSearch` handles awaiting a batched release — see
        #: `_discard_search`.
        self._pending_searches: list[str] = []
        #: Timing-only adaptive navigation history. It is intentionally attached to this
        #: Tab/CDP session, contains no origin or content, and is discarded on reattach.
        self._navigation_history_lock = threading.Lock()
        self._navigation_history: deque[
            tuple[float | None, float | None, float | None]
        ] = deque(maxlen=NAVIGATION_HISTORY_MAX)
        #: Factual Network-domain evidence for the read-only endpoint planner. Selection
        #: policy lives in ops/batch.py; the Tab only pairs request/response events and
        #: records whether Chrome supplied complete credential evidence. This lock is
        #: separate from waiter/dialog state so a snapshot cannot delay event delivery.
        self._endpoint_lock = threading.Lock()
        self._endpoint_pending: dict[str, dict[str, Any]] = {}
        self._endpoint_pending_order: deque[str] = deque()
        self._endpoint_observations: deque[dict[str, Any]] = deque(
            maxlen=ENDPOINT_OBSERVATION_LIMIT)
        self._endpoint_sequence = 0
        self._endpoint_request_sequence = 0
        self._endpoint_document_url = ""
        self._endpoint_document_generation = 0
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
            with self._navigation_history_lock:
                self._navigation_history.clear()
            self._rearm_session(sid)
        else:
            self._session_id = sid
        return sid

    def _rearm_session(self, sid: str) -> None:
        """Reinstall per-session state on a replacement session, quietly but journaled."""
        self._j.write("note", event="session_rearmed", target_id=self.target_id)
        prepared = self._reg.prepare_runtime(self.target_id)
        if prepared.session_id != sid:
            self._session_id = prepared.session_id

    def _install_runtime(self) -> None:
        """Item 18: the registry + mutation counter exist on every document this tab will
        ever load, so refs survive navigation by reinstallation, not by luck — and they
        live in an isolated world, so the page never sees them."""
        prepared = self._reg.prepare_runtime(self.target_id)
        self._session_id = prepared.session_id
        # Do not create an isolated world here. Read-only navigation, page digests,
        # screenshots, and raw main-world JS do not use it. The first ref/wait operation
        # creates it lazily, avoiding frame-tree/world calls in research-only processes.

    def _ensure_world(self) -> int | None:
        """Isolated-world context id for the main frame, created on demand.

        Worlds die with their document, so this is re-resolved rather than cached across
        main-frame navigations; `executionContextsCleared` and a main-frame
        `Page.frameNavigated` drop the stale id (see `_on_event`).

        The frame id itself is NOT re-resolved. `Page.getFrameTree` was issued on every
        rebuild to read one field — `frameTree.frame.id` — out of a reply that carries the
        whole tree (~750 bytes on a real posting), while `Page.frameNavigated` already
        hands the reader thread that same id for free.
        """
        if self._world_ctx is not None:
            return self._world_ctx
        sid = self._sid()
        try:
            frame = self._main_frame or self._conn.request(
                "Page.getFrameTree", session_id=sid,
                timeout=10.0)["frameTree"]["frame"]["id"]
            self._main_frame = frame
            ctx = self._conn.request(
                "Page.createIsolatedWorld",
                {"frameId": frame, "worldName": WORLD, "grantUniveralAccess": True},
                session_id=sid, timeout=10.0)["executionContextId"]
        except HarnessError:
            # A cached frame id Chrome no longer recognises is the one new way this can
            # fail, and it must not become permanent: forgetting it costs one
            # `Page.getFrameTree` on the next call — which is what every call used to pay.
            self._main_frame = None
            return None            # degrade to the main world rather than fail the call
        # No `Runtime.evaluate(RUNTIME_JS)` here any more. Measured against Chrome
        # 151.0.7922.174 over raw CDP: `createIsolatedWorld(worldName=W)` returns the SAME
        # world `addScriptToEvaluateOnNewDocument(worldName=W)` populates — a counter set
        # by the registered script read back as 1 through the freshly "created" world,
        # both for an already-loaded document (`runImmediately`, the attach path) and for a
        # document loaded afterwards (the rebuild path); a second createIsolatedWorld
        # returned the identical context id. So the injection was a round trip that
        # re-ran an idempotent script over itself. That is Chrome behaviour and not a
        # protocol guarantee, so `_world_js` heals a world that does come back empty.
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
        try:
            return _unwrap_eval(r)
        except JsException as original:
            # An isolated world the document-start registration did not populate is the one
            # failure here we can repair, and injecting on that signal rather than before
            # every call moves the cost from every rebuild to the Chrome builds that
            # actually need it. What we must NOT do is guess the symptom: keying on
            # "__bh is not defined" looked precise and was unreachable, because this
            # module's own JS either self-creates `__bh` (`window.__bh || (window.__bh =
            # ...)`) or guards it, so an empty world answers with a TypeError about
            # something else entirely — never that ReferenceError. Matching prose for a
            # condition is the `str`-typed error disease D11 exists to kill.
            #
            # `_world_js` only ever runs THIS module's JS, never a caller's, so any
            # exception here is harness JS failing. Re-running the idempotent registration
            # once is cheap and safe; a genuine bug in that JS simply fails the same way
            # again, and then the caller gets the ORIGINAL cause, not the retry's (rule 2).
            ctx = params["contextId"]
            if ctx in self._healed_worlds:
                raise                      # already repopulated: this is a real JS bug
            self._healed_worlds.add(ctx)
            try:
                self.cdp("Runtime.evaluate",
                         {"expression": RUNTIME_JS, "contextId": ctx}, timeout=timeout)
                return _unwrap_eval(self.cdp("Runtime.evaluate", params, timeout=timeout))
            except HarnessError:
                raise original from None

    def _owned_page_creation(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """The page target causally opened by this tab, if ``msg`` announces one.

        ``Target.targetCreated`` is browser-level and therefore has no session id. Every
        place that treats it as this tab's consequence must use this opener predicate;
        filtering only the stored delta still lets a foreign event terminate a wait.
        """
        if msg.get("method") != "Target.targetCreated":
            return None
        info = ((msg.get("params") or {}).get("targetInfo") or {})
        if info.get("type") == "page" and info.get("openerId") == self.target_id:
            return info
        return None

    @staticmethod
    def _endpoint_auth_headers(headers: Any) -> bool:
        """Whether a CDP header map explicitly carries request/response credentials.

        Header *values* never cross this boundary. Network ExtraInfo events can contain
        cookies and bearer tokens, so retaining the original mapping would turn a bounded
        endpoint index into a secret store.
        """
        if not isinstance(headers, dict):
            return False
        names = {str(name).strip().lower() for name in headers}
        return bool(names & {
            "authorization", "proxy-authorization", "cookie",
            "set-cookie", "www-authenticate", "proxy-authenticate",
            "authentication-info", "proxy-authentication-info",
            "x-api-key", "x-auth-token", "x-csrf-token", "x-xsrf-token",
        })

    @staticmethod
    def _endpoint_private_response(headers: Any) -> bool:
        if not isinstance(headers, dict):
            return False
        normalized = {
            str(name).strip().lower(): str(value).strip().lower()
            for name, value in headers.items()
        }
        cache_control = normalized.get("cache-control", "")
        vary = normalized.get("vary", "")
        return (
            "private" in {part.strip() for part in cache_control.split(",")}
            or "no-store" in {part.strip() for part in cache_control.split(",")}
            or bool({part.strip() for part in vary.split(",")} & {
                "cookie", "authorization", "x-api-key", "x-auth-token",
            })
        )

    @staticmethod
    def _bounded_endpoint_url(value: Any) -> tuple[str, bool]:
        raw = str(value or "")
        return raw[:ENDPOINT_URL_LIMIT], len(raw) > ENDPOINT_URL_LIMIT

    def _endpoint_entry(self, request_id: str) -> dict[str, Any]:
        entry = self._endpoint_pending.get(request_id)
        if entry is not None:
            return entry
        entry = {
            "request_id": request_id,
            "request_seen": False,
            "request_extra_seen": False,
            "request_extra_complete": False,
            "request_credentials": False,
            "response_seen": False,
            "response_extra_seen": False,
            "response_auth": False,
            "redirected": False,
        }
        self._endpoint_pending[request_id] = entry
        self._endpoint_pending_order.append(request_id)
        while len(self._endpoint_pending) > ENDPOINT_PENDING_LIMIT:
            oldest = self._endpoint_pending_order.popleft()
            self._endpoint_pending.pop(oldest, None)
        return entry

    def _observe_endpoint_event(self, method: str, params: dict[str, Any]) -> None:
        """Pair bounded request facts on the reader thread; never make a CDP request."""
        if method == "Page.frameNavigated":
            frame = params.get("frame") or {}
            if frame.get("parentId"):
                return
            document_url, _ = self._bounded_endpoint_url(frame.get("url"))
            with self._endpoint_lock:
                # A full main-frame navigation invalidates the old page's endpoint plan,
                # including same-origin navigations. Otherwise a later helper could replay
                # evidence from a document it never inspected.
                self._endpoint_pending.clear()
                self._endpoint_pending_order.clear()
                self._endpoint_observations.clear()
                self._endpoint_sequence = 0
                self._endpoint_request_sequence = 0
                self._endpoint_document_url = document_url
                self._endpoint_document_generation += 1
            return
        if method == "Page.navigatedWithinDocument":
            frame_id = str(params.get("frameId") or "")
            if self._main_frame and frame_id and frame_id != self._main_frame:
                return
            document_url, _ = self._bounded_endpoint_url(params.get("url"))
            with self._endpoint_lock:
                # pushState/hash navigation changes the current route's evidence scope.
                # Keeping endpoints observed on the previous route would be a semantic
                # guess about this SPA, so require fresh Network evidence instead.
                self._endpoint_pending.clear()
                self._endpoint_pending_order.clear()
                self._endpoint_observations.clear()
                self._endpoint_sequence = 0
                self._endpoint_request_sequence = 0
                self._endpoint_document_url = document_url
                self._endpoint_document_generation += 1
            return

        request_id = str(params.get("requestId") or "")
        if not request_id or method not in {
            "Network.requestWillBeSent",
            "Network.requestWillBeSentExtraInfo",
            "Network.responseReceived",
            "Network.responseReceivedExtraInfo",
        }:
            return
        with self._endpoint_lock:
            entry = self._endpoint_entry(request_id)
            if method == "Network.requestWillBeSent":
                request = params.get("request") or {}
                if entry.get("request_seen"):
                    # CDP reuses requestId across redirects. Pairing the several ExtraInfo
                    # events is order-sensitive, so the automatic path refuses the chain.
                    entry["redirected"] = True
                else:
                    self._endpoint_request_sequence += 1
                    entry["request_sequence"] = self._endpoint_request_sequence
                url, truncated = self._bounded_endpoint_url(request.get("url"))
                document_url, document_truncated = self._bounded_endpoint_url(
                    params.get("documentURL"))
                entry.update({
                    "request_seen": True,
                    "url": url,
                    "url_truncated": truncated,
                    "method": str(request.get("method") or "").upper(),
                    "has_post_data": bool(request.get("hasPostData")
                                          or request.get("postData")),
                    "request_credentials": bool(entry.get("request_credentials"))
                                           or self._endpoint_auth_headers(
                                               request.get("headers")),
                    "resource_type": str(params.get("type") or ""),
                    "frame_id": str(params.get("frameId") or ""),
                    "document_url": document_url,
                    "document_url_truncated": document_truncated,
                    "redirected": bool(entry.get("redirected")
                                       or params.get("redirectResponse")),
                })
            elif method == "Network.requestWillBeSentExtraInfo":
                headers = params.get("headers")
                cookies = params.get("associatedCookies")
                entry["request_extra_seen"] = True
                entry["request_extra_complete"] = (
                    isinstance(headers, dict) and isinstance(cookies, list))
                # Refuse even blocked associated cookies. That is deliberately stricter
                # than "was a Cookie header sent": the page is in credential-bearing
                # state, and automatic replay is only for unambiguous public reads.
                entry["request_credentials"] = bool(
                    entry.get("request_credentials")
                    or self._endpoint_auth_headers(headers)
                    or (isinstance(cookies, list) and cookies)
                )
            elif method == "Network.responseReceivedExtraInfo":
                entry["response_extra_seen"] = True
                entry["response_auth"] = bool(
                    entry.get("response_auth")
                    or self._endpoint_auth_headers(params.get("headers")))
                entry["response_private"] = bool(
                    entry.get("response_private")
                    or self._endpoint_private_response(params.get("headers")))
            else:
                response = params.get("response") or {}
                response_url, response_url_truncated = self._bounded_endpoint_url(
                    response.get("url"))
                entry.update({
                    "response_seen": True,
                    "response_url": response_url,
                    "response_url_truncated": response_url_truncated,
                    "status": int(response.get("status") or 0),
                    "mime_type": str(response.get("mimeType") or "").lower()[:200],
                    "response_extra_expected": bool(params.get("hasExtraInfo")),
                    "response_auth": bool(entry.get("response_auth"))
                                   or self._endpoint_auth_headers(response.get("headers")),
                    "response_private": bool(entry.get("response_private"))
                                      or self._endpoint_private_response(
                                          response.get("headers")),
                    "from_service_worker": bool(response.get("fromServiceWorker")),
                })
                if not entry.get("observation_sequence"):
                    self._endpoint_sequence += 1
                    entry["observation_sequence"] = self._endpoint_sequence
                    # Keep the same bounded dict alive in the pending table so an
                    # ExtraInfo event that follows responseReceived can complete it.
                    self._endpoint_observations.append(entry)

    def _endpoint_snapshot(self) -> dict[str, Any]:
        """Internal factual snapshot consumed by the bounded endpoint planner.

        No header/body values are present. Returning copies prevents the reader thread's
        later ExtraInfo event from mutating a plan while it is being selected.
        """
        with self._endpoint_lock:
            rows = [dict(row) for row in self._endpoint_observations]
            rows.sort(key=lambda row: (
                int(row.get("request_sequence") or 2**63 - 1),
                int(row.get("observation_sequence") or 0),
            ))
            return {
                "document_url": self._endpoint_document_url,
                "document_generation": self._endpoint_document_generation,
                "main_frame": self._main_frame or "",
                "observation_limit": ENDPOINT_OBSERVATION_LIMIT,
                "observations_dropped": max(
                    0, self._endpoint_sequence - len(self._endpoint_observations)),
                "observations": rows,
            }

    def _on_event(self, msg: dict[str, Any]) -> None:
        """Reader thread: bookkeeping and waiter wakeups only, never a request."""
        sid = msg.get("sessionId")
        if sid is not None and sid != self._session_id:
            return                                     # another tab's event
        method = msg.get("method", "")
        params = msg.get("params") or {}
        self._observe_endpoint_event(method, params)
        if self._diagnostics_enabled:
            diagnostic = self._sanitize_diagnostic_event(method, params)
            if diagnostic is not None:
                self._diagnostic_events.append(diagnostic)
        if method == "Runtime.executionContextsCleared":
            self._world_ctx = None            # the world died with its document
        elif method == "Page.frameNavigated":
            # Only the MAIN frame's navigation replaces the document our world lives in.
            # Subframes announce themselves through this same event — with `parentId` set —
            # and an ATS posting fires it for every ad, tracker and embedded video it
            # loads: measured over one run, 149 navigations produced 233 invalidations, so
            # 84 of the rebuilds resurrected a world that had never died. Absent params
            # (or an absent frame) stay on the conservative side and invalidate.
            frame = params.get("frame") or {}
            if not frame.get("parentId"):
                self._world_ctx = None
                # The id `_ensure_world` used to buy with a whole frame tree, for free.
                self._main_frame = frame.get("id") or self._main_frame
        if method == "Page.javascriptDialogOpening":
            with self._wlock:
                self._dialog = params
            # EVERY open dialog blocks the renderer for every caller — Page.navigate
            # stops answering, Runtime.evaluate stops answering — and only a click's own
            # dialog dance knew how to resolve one. A dialog that opened any other way
            # (beforeunload on navigation, an alert from a page timer) wedged the tab for
            # good: measured under parallel() as one worker per run dying at exactly
            # 45.0s — a 25s navigate timeout plus a 20s evaluate timeout on a renderer
            # that would never answer either. So no dialog goes unresolved: beforeunload
            # is accepted immediately (it is not an application action; accepting only
            # permits the navigation the caller already requested), and the rest get a
            # grace period for the click dance to claim them before the accept_dialogs
            # policy applies. Off the reader thread, which must never make a request.
            threading.Thread(
                target=self._resolve_dialog,
                args=(params,),
                name="bh-dialog",
                daemon=True,
            ).start()
        elif method == "Target.targetCreated":
            # `Target.targetCreated` is a BROWSER-level event: it carries no sessionId, so
            # the session short-circuit at the top of this method never sees it and cannot
            # filter it. Without the opener check every Tab in the process recorded every
            # page target the whole browser opened — the other `parallel()` workers' tabs,
            # other `bh` processes' tabs, the user's own browsing — and `follow_application`
            # does `use_tab(new_targets[-1])`, so worker A went on to fill its form in
            # worker B's tab. The same leak told the inert-click guard that a click which
            # had done nothing had opened a tab.
            #
            # `openerId` is the causal link and it is reliable here for the reason
            # `_owned_tab_descendants` in ops/parallel.py documents: Chrome retains it even
            # for `rel=noopener` targets while the opener is alive (measured with
            # `canAccessOpener=false`).
            info = self._owned_page_creation(msg)
            if info is not None:
                self._created_seq += 1
                self._created.append((self._created_seq, info))
        with self._wlock:
            waiters = list(self._waiters)
        for w in waiters:
            w.offer(msg)

    #: How long a click's own dialog dance gets to claim an alert/confirm/prompt before
    #: the auto-resolver applies the accept_dialogs policy. Much shorter than the 2s the
    #: dance waits on its blocked dispatch, so the dance never loses the race when it is
    #: actually running — and much shorter than any caller's timeout, so an uninvited
    #: dialog costs a quarter second instead of wedging the tab until the run ends.
    DIALOG_GRACE = 0.25

    def _resolve_dialog(self, pending: dict[str, Any]) -> None:
        kind = str(pending.get("type") or "")
        accept = True if kind == "beforeunload" else self.accept_dialogs
        if kind != "beforeunload":
            time.sleep(self.DIALOG_GRACE)
            with self._wlock:
                if self._dialog is not pending:
                    return          # the click dance claimed and resolved it
        try:
            self.cdp("Page.handleJavaScriptDialog", {"accept": accept}, timeout=5.0)
            self._j.write("note", event="dialog_auto_handled", dialog_type=kind,
                          accepted=accept, target_id=self.target_id,
                          message=str(pending.get("message") or "")[:120])
        except HarnessError as exc:
            # Chrome can close the dialog itself between the event and our command.
            if "No dialog is showing" not in str(exc):
                self._j.write("note", event="dialog_handle_failed", dialog_type=kind,
                              error_class=exc.cls.value, target_id=self.target_id)
        finally:
            with self._wlock:
                if self._dialog is pending:
                    self._dialog = None
                    # The dance may still be waiting on its blocked dispatch; this record
                    # is how it learns that its click DID open a dialog and that the
                    # dialog is already resolved — reported, never re-dismissed.
                    self._auto_dialog = (time.monotonic(), pending)

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
        # Domains persist for the CDP session, just like injected scripts. Ask the daemon's
        # registry to enable these once instead of paying two CDP calls in every process.
        try:
            self._reg.ensure_domains(self.target_id, ("Log", "Performance"))
            enabled.extend(("Log", "Performance"))
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
        # Resource aggregation and the event-loop probe both read the same Performance
        # timeline. One evaluation returns both, rather than rebuilding an isolated world
        # and crossing CDP twice. This is observability code, so keep it in the main world
        # and independent of the harness runtime.
        try:
            probe = self._main_js("""await new Promise(resolve => {
              const start = performance.now();
              setTimeout(() => {
                const rows = performance.getEntriesByType('resource');
                const kinds = {}; let transfer = 0; let longest = 0;
                for (const r of rows) { const k = r.initiatorType || 'other';
                  kinds[k] = (kinds[k] || 0) + 1; transfer += r.transferSize || 0;
                  longest = Math.max(longest, r.duration || 0); }
                resolve({resources: {count: rows.length, by_type: kinds,
                         transfer_bytes: transfer, longest_ms: Math.round(longest)},
                         event_loop_ms: performance.now() - start});
              }, 0);
            })""", timeout=5.0) or {}
            resources = probe.get("resources") or {}
            event_loop_ms = float(probe.get("event_loop_ms") or 0)
        except (HarnessError, AttributeError, TypeError, ValueError):
            resources, event_loop_ms = {}, 0.0
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

    def _adaptive_navigation_grace(self, configured: float) -> tuple[float, int]:
        """Return this session's bounded usable-document grace and sample count.

        Samples are timing triples only: parsed, exact lifecycle, and network-quiescence
        offsets. They are neither keyed by nor persisted with an origin. The maximum of
        recent completion milestones deliberately adapts upward faster than downward.
        """
        value = float(configured)
        if not math.isfinite(value):
            raise ValueError("usable_after must be a finite number or None")
        ceiling = max(0.0, value)
        with self._navigation_history_lock:
            samples = list(self._navigation_history)
        if len(samples) < NAVIGATION_HISTORY_MIN:
            return ceiling, len(samples)
        milestones = [max(v for v in sample if v is not None) for sample in samples
                      if any(v is not None for v in sample)]
        if not milestones:
            return ceiling, len(samples)
        learned = min(NAVIGATION_GRACE_MAX,
                      max(NAVIGATION_GRACE_MIN,
                          max(milestones) + NAVIGATION_GRACE_MARGIN))
        # A caller-supplied value below the adaptive floor remains authoritative.
        return min(ceiling, learned), len(samples)

    def _remember_navigation_timing(self, parsed: float | None,
                                    lifecycle: float | None,
                                    network_quiet: float | None) -> None:
        if lifecycle is None:
            return                         # censored fallback samples never lower the grace
        with self._navigation_history_lock:
            self._navigation_history.append((parsed, lifecycle, network_quiet))

    def goto(self, url: str, *, timeout: float = 20.0, wait_until: str = "load",
             usable_after: float | None = 0.8, digest: bool = False,
             max_chars: int = 6_000, max_links: int = 20,
             content_only: bool = True) -> dict[str, Any]:
        """Returns `{requested, landed, lifecycle}` or raises `NavigationFailed`/`Timeout`.
        A 404 error page cannot be reported as a title (v1 did exactly that).

        `lifecycle` says which condition ended the wait, and there are four:

        | value       | meaning |
        |-------------|---------|
        | `"load"`    | the requested event arrived — the normal case |
        | `"settled"` | it did not, but the document parsed and the network went quiet |
        | `"usable"`  | after `usable_after`, the parsed document already had content |
        | `"timeout"` | neither, yet the document was usable at the final deadline |

        The last two exist because *one stalled subresource holds `load` forever*. A single
        image, stylesheet or iframe that is accepted and never answered is enough: measured
        on all three, `DOMContentLoaded` fires, the form is in the DOM and fillable, paint
        completes — and `load` never comes. The old code waited out the full timeout and
        then raised, discarding a page that had been ready for seconds.

        That is not hypothetical. Across three live runs every single `goto` failure — 14
        of 14, 505 seconds — was one host tarpitting its subresources. Those pages were
        usable: the caller caught the `Timeout`, carried on, and filled the forms anyway.
        The harness spent 505 seconds proving the caller right.

        A numeric ``usable_after`` is an upper bound for a session-local adaptive grace.
        After two exact navigations the timing-only history may reduce it, clamped to
        0.5–3.0 seconds.

        The bound was 3.0s and is 0.8s, measured over four 100-job runs of the same corpus.
        Navigation is 88% of an attempt's wall clock, and two thirds of navigations were
        waiting for `load` on a document that a third of readiness checks then found
        already usable. Lowering it cut navigation time by 18% and 42% in two replicates —
        a spread wide enough that the size of the win is not worth quoting, only its
        direction — while both replicates discovered *more* fields and processed more
        forms than the 3.0s control, and neither produced a workflow failure where the
        control produced one. Lowering the bound alone is not safe: it is only sound
        together with the repeating readiness probe below, without which four pages that
        render from already-fetched script became `load` timeouts. An explicit value below 0.5 seconds remains authoritative. An
        early ``usable`` or ``settled`` result additionally requires no observed XHR,
        fetch, or event stream in flight, a 150 ms network-quiet window, and two identical
        bounded document probes 150 ms apart. A parsed SPA shell is therefore evidence to
        keep waiting, not a complete-page claim.

        Set `usable_after=None` when the exact lifecycle event is a hard requirement. It
        disables both early fallbacks; the deadline retains the existing honest
        ``lifecycle="timeout"`` usable-document outcome.
        `digest=True` folds a bounded page read into the URL check, so `open_page()` costs
        no more CDP round trips than `goto()` followed by its ordinary landing check.
        """
        seen: set[str] = set()
        loader = None
        lifecycle = "timeout"
        wait_started = time.perf_counter()
        deadline = wait_started + timeout
        strict = usable_after is None
        configured_grace = None if strict else float(usable_after)
        if strict:
            effective_grace = None
            with self._navigation_history_lock:
                history_samples = len(self._navigation_history)
        else:
            effective_grace, history_samples = self._adaptive_navigation_grace(
                configured_grace)
        exact: dict[str, Any] | None = None
        lifecycle_times: dict[str, float] = {}
        network_quiet_at: float | None = None
        readiness_at: float | None = None
        readiness_probes = 0
        critical_peak = 0
        blocked_by_data = False
        fallback = False
        fallback_kind = ""

        with self._j.call("goto", url=url), \
             self._armed(lambda m: m.get("method") in NAVIGATION_EVENTS) as w:
            nav = self.cdp("Page.navigate", {"url": url}, timeout=timeout)
            if err := nav.get("errorText"):
                raise NavigationFailed(err, requested=url, landed=self._try_url())
            loader = nav.get("loaderId")
            frame_id = nav.get("frameId")
            fallback_enabled = bool(loader) and wait_until == "load" and not strict
            cursor = 0
            requests: dict[str, str] = {}
            critical: set[str] = set()
            last_network_activity = wait_started
            activity_epoch = 0
            settled_seen = False
            first_probe: tuple[float, tuple[Any, ...], int] | None = None
            last_probe_epoch: int | None = None
            reprobe_at: float | None = None
            reprobe_wait = NAVIGATION_REPROBE

            def consume(item: tuple[float, dict[str, Any]]) -> None:
                nonlocal exact, network_quiet_at, last_network_activity
                nonlocal activity_epoch, settled_seen, first_probe, critical_peak
                nonlocal reprobe_at, reprobe_wait
                timestamp, message = item
                method = message.get("method")
                params = message.get("params") or {}
                if method == "Page.lifecycleEvent":
                    name = params.get("name")
                    if not isinstance(name, str):
                        return
                    if loader:
                        if params.get("loaderId") != loader:
                            return
                    elif name != wait_until:
                        return                  # unidentifiable: exact match only
                    seen.add(name)
                    lifecycle_times.setdefault(name, max(0.0, timestamp - wait_started))
                    if name == wait_until:
                        exact = message
                    if name in {"networkAlmostIdle", "networkIdle"}:
                        network_quiet_at = max(0.0, timestamp - wait_started)
                        last_network_activity = timestamp
                        activity_epoch += 1
                        first_probe = None
                        reprobe_at = None
                        reprobe_wait = NAVIGATION_REPROBE
                    if (fallback_enabled
                            and {"DOMContentLoaded", "networkAlmostIdle"} <= seen):
                        settled_seen = True
                    return
                if not loader:
                    return
                request_id = str(params.get("requestId") or "")
                if method == "Network.requestWillBeSent":
                    event_loader = params.get("loaderId")
                    belongs = (event_loader == loader if event_loader else
                               bool(frame_id) and params.get("frameId") == frame_id)
                    if not belongs or not request_id:
                        return
                    kind = str(params.get("type") or "Other")
                    requests[request_id] = kind
                    critical.discard(request_id)       # redirects reuse a request id
                    if kind in NAVIGATION_DATA_TYPES:
                        critical.add(request_id)
                        critical_peak = max(critical_peak, len(critical))
                elif method in {"Network.loadingFinished", "Network.loadingFailed"}:
                    if request_id not in requests:
                        return
                    requests.pop(request_id, None)
                    critical.discard(request_id)
                    if not requests:
                        network_quiet_at = max(0.0, timestamp - wait_started)
                else:
                    return
                last_network_activity = timestamp
                activity_epoch += 1
                first_probe = None
                reprobe_at = None
                reprobe_wait = NAVIGATION_REPROBE

            def take_event(wait: float) -> bool:
                nonlocal cursor
                cursor, item = w.wait_next(cursor, wait)
                if item is None:
                    return False
                consume(item)
                while True:
                    cursor, buffered = w.wait_next(cursor, 0.0)
                    if buffered is None:
                        break
                    consume(buffered)
                return True

            grace_at = (wait_started + effective_grace
                        if effective_grace is not None else deadline)
            while exact is None and time.perf_counter() < deadline:
                take_event(0.0)
                if exact is not None:
                    break
                now = time.perf_counter()
                grace_elapsed = now >= grace_at
                may_probe = fallback_enabled and (settled_seen or grace_elapsed)
                quiet = now - last_network_activity >= NAVIGATION_QUIET
                if critical:
                    blocked_by_data = blocked_by_data or may_probe
                due_probe = may_probe and not critical and quiet and (
                    (first_probe is not None
                     and now - first_probe[0] >= NAVIGATION_STABLE)
                    or (first_probe is None and last_probe_epoch != activity_epoch)
                    # Ask again on a timer as well as on new traffic, so a document that
                    # renders from script it already holds is not judged once and dropped.
                    or (first_probe is None and reprobe_at is not None
                        and now >= reprobe_at)
                )
                if due_probe:
                    epoch_before = activity_epoch
                    usable, signature = self._document_readiness(
                        min(2.0, max(0.1, deadline - time.perf_counter())))
                    readiness_probes += 1
                    probe_time = time.perf_counter()
                    take_event(0.0)              # exact/data events that raced the probe
                    if exact is not None:
                        break
                    last_probe_epoch = activity_epoch
                    if (critical or activity_epoch != epoch_before
                            or probe_time - last_network_activity < NAVIGATION_QUIET):
                        first_probe = None
                    elif usable:
                        if (first_probe is not None
                                and first_probe[1] == signature
                                and first_probe[2] == activity_epoch):
                            fallback = True
                            fallback_kind = "settled" if settled_seen else "usable"
                            readiness_at = max(0.0, probe_time - wait_started)
                            break
                        first_probe = (probe_time, signature, activity_epoch)
                    else:
                        first_probe = None
                        reprobe_at = probe_time + reprobe_wait
                        reprobe_wait = min(NAVIGATION_REPROBE_MAX, reprobe_wait * 2)
                    continue

                wake_at = deadline
                if fallback_enabled:
                    if not (settled_seen or grace_elapsed):
                        wake_at = min(wake_at, grace_at)
                    elif not critical:
                        if not quiet:
                            wake_at = min(wake_at,
                                          last_network_activity + NAVIGATION_QUIET)
                        elif first_probe is not None:
                            wake_at = min(wake_at,
                                          first_probe[0] + NAVIGATION_STABLE)
                        elif last_probe_epoch != activity_epoch:
                            wake_at = now
                        elif reprobe_at is not None:
                            wake_at = min(wake_at, reprobe_at)
                take_event(max(0.0, wake_at - time.perf_counter()))

            # Drain events already ordered ahead of the final readiness evaluation. Exact
            # lifecycle always wins, including when it arrives at the deadline boundary.
            take_event(0.0)
            if exact is not None:
                lifecycle = "load"
                if wait_until == "load":
                    self._remember_navigation_timing(
                        lifecycle_times.get("DOMContentLoaded"),
                        lifecycle_times.get(wait_until), network_quiet_at)
            elif fallback:
                lifecycle = fallback_kind
            else:
                usable = self._usable_document(min(timeout, 5.0))
                take_event(0.0)
                if exact is not None:
                    lifecycle = "load"
                    if wait_until == "load":
                        self._remember_navigation_timing(
                            lifecycle_times.get("DOMContentLoaded"),
                            lifecycle_times.get(wait_until), network_quiet_at)
                elif not usable:
                    raise Timeout(f"no {wait_until!r} lifecycle event in {timeout}s",
                                  requested=url, wait_until=wait_until,
                                  lifecycle_seen=sorted(seen))

        waited = max(0.0, time.perf_counter() - wait_started)
        adaptive_saved_ms = (0.0 if configured_grace is None or effective_grace is None
                             else max(0.0, configured_grace - effective_grace) * 1000)
        fallback_saved_ms = (0.0 if not fallback or configured_grace is None
                             else max(0.0, configured_grace - waited) * 1000)
        self._j.write("note", event="navigation_wait", target_id=self.target_id,
                      lifecycle=lifecycle, lifecycle_seen=sorted(seen),
                      wait_ms=round(waited * 1000, 1), usable_after=usable_after,
                      effective_usable_after=effective_grace,
                      adaptive_samples=history_samples,
                      adaptive_saved_ms=round(adaptive_saved_ms, 1),
                      fallback_saved_ms=round(fallback_saved_ms, 1),
                      parsed_ready_ms=round(
                          lifecycle_times.get("DOMContentLoaded", 0.0) * 1000, 1)
                          if "DOMContentLoaded" in lifecycle_times else None,
                      exact_lifecycle_ms=round(
                          lifecycle_times.get(wait_until, 0.0) * 1000, 1)
                          if wait_until in lifecycle_times else None,
                      network_quiet_ms=round(network_quiet_at * 1000, 1)
                          if network_quiet_at is not None else None,
                      readiness_stable_ms=round(readiness_at * 1000, 1)
                          if readiness_at is not None else None,
                      readiness_probes=readiness_probes,
                      critical_requests_peak=critical_peak,
                      blocked_by_data=blocked_by_data)
        page = self._page_digest(max_chars=max_chars, max_links=max_links,
                                 content_only=content_only) if digest else None
        landed = str((page or {}).get("url") or self._try_url() or url)
        if landed.startswith("chrome-error://"):
            raise NavigationFailed("landed on an error page", requested=url, landed=landed)
        result = {"requested": url, "landed": landed, "lifecycle": lifecycle}
        if digest:
            result["page"] = page or {}
        return result

    def open_page(self, url: str, *, timeout: float = 20.0,
                  wait_until: str = "load", usable_after: float | None = 3.0,
                  max_chars: int = 6_000, max_links: int = 20,
                  content_only: bool = True) -> dict[str, Any]:
        """Navigate and return a bounded research-ready page digest in one helper call."""
        return self.goto(url, timeout=timeout, wait_until=wait_until,
                         usable_after=usable_after, digest=True,
                         max_chars=max_chars, max_links=max_links,
                         content_only=content_only)

    def _document_readiness(self, timeout: float) -> tuple[bool, tuple[Any, ...]]:
        """Return factual usability plus a bounded, content-free stability signature.

        The bar deliberately excludes `readyState === 'loading'`: a parser that has not
        finished may still be about to produce the form, and returning then would hand the
        caller a half-built document that reads as an empty page. The signature samples at
        most 16 KiB of rendered text into a 32-bit hash and reports only counts/hash — never
        page text or form values. Two equal probes are evidence of observed stability, not
        a semantic assertion about what the page ought to contain.
        """
        try:
            value = self._main_js("""(() => {
              const text = ((document.body && document.body.innerText) || '').trim();
              const sample = text.length <= 16384 ? text
                : text.slice(0, 8192) + text.slice(-8192);
              let hash = 2166136261;
              for (let i = 0; i < sample.length; i++) {
                hash ^= sample.charCodeAt(i); hash = Math.imul(hash, 16777619);
              }
              return [document.readyState,
                document.querySelectorAll('input,textarea,select,button').length,
                text.length, hash >>> 0,
                document.getElementsByTagName('*').length];
            })()""", timeout=min(timeout, 5.0))
        except HarnessError:
            return False, ()
        if not isinstance(value, (list, tuple)) or len(value) < 3:
            return False, ()
        try:
            state, controls, text = str(value[0]), int(value[1]), int(value[2])
        except (TypeError, ValueError):
            return False, ()
        signature = (state, controls, text, *tuple(value[3:5]))
        return state != "loading" and (controls > 0 or text > 0), signature

    def _usable_document(self, timeout: float) -> bool:
        """Compatibility wrapper for the final-deadline usable-document check."""
        return self._document_readiness(timeout)[0]

    def wait_lifecycle(self, name: str = "networkIdle", *, timeout: float = 10.0) -> None:
        with self._armed(lambda m: m.get("method") == "Page.lifecycleEvent") as w:
            if w.wait_match(lambda m: (m.get("params") or {}).get("name") == name,
                            timeout) is None:
                raise Timeout(f"no {name!r} lifecycle event in {timeout}s", wait=name)

    def _try_url(self) -> str | None:
        try:
            return self._main_js("location.href", timeout=5.0)
        except HarnessError:
            return None

    def _action_consequence(self, marker: int | str, *, refs: list[str] | None = None,
                            before_states: dict[str, Any] | None = None,
                            after_states: dict[str, Any] | None = None,
                            timeout: float = 10.0) -> dict[str, Any]:
        """Bounded semantic/validation evidence since one mechanical action began."""
        source = self._action_consequence_source(marker, refs=refs)
        try:
            observed = self._world_js(source, timeout=timeout)
        except HarnessError:
            observed = {}
        return self._shape_action_consequence(
            observed, before_states=before_states, after_states=after_states)

    @staticmethod
    def _action_consequence_source(marker: int | str,
                                   *, refs: list[str] | None = None) -> str:
        refs = [str(ref) for ref in (refs or []) if ref]
        return (_ACTION_CONSEQUENCE_JS
                .replace("__MARKER__", json.dumps(marker))
                .replace("__REFS__", json.dumps(refs)))

    @staticmethod
    def _shape_action_consequence(observed: Any, *,
                                  before_states: dict[str, Any] | None = None,
                                  after_states: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(observed, dict):
            observed = {}
        current = dict(observed.get("states") or {})
        current.update(after_states or {})
        prior = dict(before_states or {})
        validation = {ref: _validation_delta(prior.get(ref), current.get(ref))
                      for ref in set(prior) | set(current)}
        changed = sorted(ref for ref, delta in validation.items() if delta["changed"])
        regions = list(observed.get("changed_regions") or [])[:12]
        modal = bool(observed.get("modal"))
        mutations = max(0, int(observed.get("mutation_count") or 0))
        related = int(observed.get("related_regions") or 0)
        if changed:
            effect = "validation"
        elif modal:
            effect = "modal"
        elif mutations or regions:
            effect = "unverified_mutation"
        else:
            effect = "none"
        return {
            "effect": effect,
            # A DOM mutation that happens during an action is temporal evidence, not
            # causality. A modal is verified only when the target mechanically names or
            # contains it; browser-level JS dialogs are promoted by click_ref itself.
            "verified": effect == "validation" or (effect == "modal" and related > 0),
            "mutation_count": mutations,
            "changed_regions": regions,
            "regions_truncated": bool(observed.get("regions_truncated")),
            "history_truncated": bool(observed.get("history_truncated")),
            "related_regions": related,
            "validation": validation,
            "validation_changed": changed,
        }

    def _action_token(self) -> str:
        return f"{self.target_id}:{self._j.next_id()}"

    def _start_action(self, token: str, *, timeout: float = 10.0) -> None:
        self._world_js(
            "(() => {const bh=window.__bh; if(!bh) return false;"
            f" if(bh.beginAction) bh.beginAction({json.dumps(token)});"
            f" else bh.actionStarts[{json.dumps(token)}]=bh.mutations||0;"
            " return true;})()",
            timeout=timeout,
        )

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
            f" return [r.x + r.width/2, r.y + r.height/2, location.href, __bh.mutations,"
            f" ({_DANGER_JS})(el), ({_CONTROL_STATE_JS})(el)];}})()",
            timeout=timeout)
        if pre is None:
            raise ElementGone(f"no element registered for ref {ref!r}", ref=ref)
        x, y, url_before, mut_before = pre[:4]
        danger = pre[4] if len(pre) > 4 else None
        control_before = pre[5] if len(pre) > 5 else None
        self._refuse_danger(danger, ref=ref)
        return self._click(x, y, url_before, int(mut_before), settle, timeout, ref=ref,
                           control_before=control_before)

    def click_at(self, x: float, y: float, *, settle: float = 0.15,
                 timeout: float = 10.0) -> dict[str, Any]:
        """Coordinate click — the default modality: compositor-level events pass through
        iframes and shadow roots that no selector can reach."""
        before = self._world_js(
            f"[location.href, window.__bh ? __bh.mutations : 0,"
            f" ({_DANGER_JS})(document.elementFromPoint({x!r}, {y!r})),"
            f" ({_CONTROL_STATE_JS})(document.elementFromPoint({x!r}, {y!r}))]",
            timeout=timeout)
        self._refuse_danger(before[2] if len(before) > 2 else None, x=x, y=y)
        return self._click(x, y, before[0], int(before[1]), settle, timeout,
                           control_before=before[3] if len(before) > 3 else None)

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

    def _targets_since(self, seq: int) -> list[str]:
        """Page targets this tab opened after `seq`, in the order Chrome announced them.

        The cursor is the sequence number, never `len(self._created)`: the buffer is
        bounded, so its length stops changing once sixteen targets have been seen, and a
        length-as-position read then reports an empty delta for every click that follows
        — for the rest of the process's life, with no error anywhere. Popups simply stop
        being followed, and the inert-click guard reads "no new target" forever.
        """
        return [str(info["targetId"]) for entry_seq, info in list(self._created)
                if entry_seq > seq and info.get("targetId")]

    def _click(self, x: float, y: float, url_before: str, mut_before: int,
               settle: float, timeout: float, ref: str | None = None,
               retry_inert: bool = True, control_before: Any = None) -> dict[str, Any]:
        interesting = ("Page.lifecycleEvent", "Page.frameNavigated",
                       "Page.javascriptDialogOpening", "Target.targetCreated")

        def click_consequence(message: dict[str, Any]) -> bool:
            method = message.get("method")
            if method not in interesting:
                return False
            # Browser-level target events carry no session id. A popup elsewhere in the
            # browser is not this click's consequence and must not end its settle window.
            return (method != "Target.targetCreated"
                    or self._owned_page_creation(message) is not None)

        seq_before = self._created_seq
        dispatch_started = time.monotonic()
        with self._j.call("click", x=x, y=y, ref=ref) , \
             self._armed(click_consequence) as w:
            for kind in ("mousePressed", "mouseReleased"):
                try:
                    self.cdp("Input.dispatchMouseEvent",
                             {"type": kind, "x": x, "y": y, "button": "left",
                              "clickCount": 1}, timeout=min(timeout, 2.0))
                except Timeout:
                    with self._wlock:
                        # Pending, OR already resolved by the auto-resolver during this
                        # very dispatch — its grace period is shorter than this wait, so
                        # by the time the timeout fires the dialog is usually gone.
                        auto = self._auto_dialog
                        blocked_by_dialog = self._dialog is not None or (
                            auto is not None and auto[0] >= dispatch_started)
                    if not blocked_by_dialog:
                        raise          # a real hang, not the dialog dance
                    break              # the click landed; its handler opened a dialog
            w.wait_match(lambda m: True, settle)       # first consequence, or settle elapses

        dialog = None
        with self._wlock:
            pending, self._dialog = self._dialog, None
            auto = self._auto_dialog
        if pending is None and auto is not None and auto[0] >= dispatch_started:
            # The auto-resolver got there first; the dialog still belongs in this click's
            # delta — same shape, already dismissed under the same accept_dialogs policy.
            dialog = {"type": auto[1].get("type"), "message": auto[1].get("message")}
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
            target = (f"window.__bh && __bh.refs[{ref!r}]" if ref is not None
                      else f"document.elementFromPoint({x!r}, {y!r})")
            post = self._world_js(
                f"[location.href, window.__bh ? __bh.mutations : 0,"
                f" ({_CONTROL_STATE_JS})({target})]", timeout=timeout)
        except HarnessError:
            pass                                       # e.g. the click closed the tab
        url_after = post[0] if post else None
        navigated = url_after is not None and url_after != url_before
        mutations = (None if navigated or post is None
                     else max(0, int(post[1]) - mut_before))
        control_after = post[2] if post and len(post) > 2 else None
        control_state_changed = _control_state_changed(control_before, control_after)
        modality = "compositor"
        if (retry_inert and not navigated and not mutations and not control_state_changed
                and dialog is None
                and not self._targets_since(seq_before)):
            landed = self._activate_click(x, y, url_before, mut_before, settle, timeout,
                                          ref=ref)
            if landed is not None:
                url_after, mutations, modality, control_after = landed
                navigated = url_after is not None and url_after != url_before
                control_state_changed = _control_state_changed(control_before, control_after)
                if dialog is None:
                    # The retried click runs the handler the dropped compositor click
                    # never reached — including one that opens a dialog. The report was
                    # finalized before the retry, so that dialog vanished from the delta
                    # (measured: hidden tab, alert-opening button — handler fired once,
                    # auto-resolver dismissed it, delta said dialog: null). Read WITHOUT
                    # popping: a still-open dialog belongs to the auto-resolver, whose
                    # `is pending` identity check a pop here would break, leaving the
                    # dialog undismissed and the tab wedged.
                    with self._wlock:
                        pending_retry = self._dialog
                        auto = self._auto_dialog
                    src = pending_retry if pending_retry is not None else (
                        auto[1] if auto is not None and auto[0] >= dispatch_started
                        else None)
                    if src is not None:
                        dialog = {"type": src.get("type"),
                                  "message": src.get("message")}
        new_targets = self._targets_since(seq_before)
        state_key = ref or "$target"
        consequence = self._action_consequence(
            mut_before,
            refs=[ref] if ref else [],
            before_states={state_key: control_before},
            after_states={state_key: control_after},
            timeout=timeout,
        )
        if navigated:
            consequence.update(effect="navigation", verified=True)
        elif dialog is not None:
            consequence.update(effect="javascript_dialog", verified=True)
        elif new_targets:
            consequence.update(effect="new_target", verified=True)
        elif control_state_changed:
            consequence.update(effect="control_state", verified=True)
        return {
            "url_before": url_before,
            "url_after": url_after,
            "navigated": navigated,
            # a new document restarts the counter, so a cross-document delta would lie
            "dom_mutations": mutations,
            "control_state_changed": control_state_changed,
            "modality": modality,
            "new_targets": new_targets,
            "dialog": dialog,
            "consequence": consequence,
        }

    def _activate_click(
        self, x: float, y: float, url_before: str, mut_before: int,
        settle: float, timeout: float, ref: str | None = None,
    ) -> tuple[Any, int | None, str, Any] | None:
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
            # Address the REF when there is one, falling back to the point. Point-hit
            # testing is right for a painted control — it hits whatever a real click
            # would — but wrong for one with no box: the centre of a zero-rect element
            # is some other element entirely, so the gesture landed on the page behind
            # the button and nothing happened (measured on the unpainted-apply fixture:
            # detection succeeded, the click did not). Runs in the ISOLATED world, which
            # shares the DOM and is where `__bh.refs` lives.
            result = self._world_js(
                "(() => {"
                f" const x = {x!r}, y = {y!r};"
                # json.dumps, not !r: a None ref renders as bare `None`, which is a
                # ReferenceError in JS and takes the whole fallback down with it.
                f" const el = (window.__bh && __bh.refs[{json.dumps(ref)}])"
                "   || document.elementFromPoint(x, y);"
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
            target = (f"window.__bh && __bh.refs[{ref!r}]" if ref is not None
                      else f"document.elementFromPoint({x!r}, {y!r})")
            post = self._world_js(
                f"[location.href, window.__bh ? __bh.mutations : 0,"
                f" ({_CONTROL_STATE_JS})({target})]", timeout=timeout)
        except HarnessError:
            return None
        url_after = post[0]
        navigated = url_after != url_before
        return (url_after, None if navigated else max(0, int(post[1]) - mut_before), "dom",
                post[2] if len(post) > 2 else None)

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
        # Whether an observer this loop armed is still running in the page. Only that case
        # needs `_unwatch`, and it is the minority: every watch script disconnects and
        # forgets its own observer before it reports a match or notifies, so the teardown
        # round trip that used to run unconditionally in `finally` was usually a
        # `Runtime.evaluate` spent asking the page to delete something already gone.
        # Measured on the 2026-08-25 corpus: 174 of `wait_for_application_state`'s 516
        # evaluations were exactly this, one per call.
        armed = False
        try:
            with self._armed(lambda message: message.get("method") == "Runtime.bindingCalled"
                             or message.get("method") in replaced) as waiter:
                while (left := deadline - time.monotonic()) > 0:
                    probe = self._world_js(
                        expression, timeout=min(max(left, 0.1), 5.0)) or {}
                    if probe.get("matched"):
                        return probe, "terminal"
                    armed = True           # the probe armed an observer and left it running
                    quiet = stable_for(probe) if stable_for is not None else left
                    hit = waiter.wait_match(interesting, min(quiet, left))
                    if hit is None:
                        return probe, "stable" if stable_for is not None and quiet <= left \
                            else "timeout"
                    consumed.add(id(hit))
                    # Both wakeups end the observer without our help: a binding means its
                    # callback ran, which disconnects and deletes it, and a cleared context
                    # or a navigated frame took the whole document — and with it the world
                    # `_unwatch` would otherwise have to rebuild in order to say so.
                    armed = False
                    if binding_finishes and hit.get("method") == "Runtime.bindingCalled":
                        return probe, "binding"
            return probe, "timeout"
        finally:
            if armed:
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

    #: How many empty search handles may go undiscarded before the tab pays one round trip
    #: to release them. Each one is a map entry holding no nodes, so the pressure is a
    #: bookkeeping entry rather than retained DOM; the cap keeps even a script that loops
    #: on one long-lived tab bounded.
    _SEARCH_FLUSH_AT = 32

    def _discard_search(self, search_id: str, *, empty: bool) -> None:
        """Release a `DOM.performSearch` handle, deferring the empty ones.

        A search that matched nothing retains nothing, so discarding it immediately buys
        no memory back — it only spends a blocking round trip, and `frames()` runs this on
        every frameless page. Measured on the 2026-08-25 corpus: 84 of 129 searches
        returned zero, at ~50ms each. Non-empty handles do hold node references and are
        still released at once.
        """
        if empty:
            self._pending_searches.append(search_id)
            if len(self._pending_searches) < self._SEARCH_FLUSH_AT:
                return
        for pending in ([search_id] if not empty else list(self._pending_searches)):
            try:
                self.cdp("DOM.discardSearchResults", {"searchId": pending}, timeout=5.0)
            except HarnessError:
                pass                       # cleanup failure cannot suppress discovery
        if empty:
            self._pending_searches.clear()

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
        # A cross-origin child is announced only while auto-attach is on, and the
        # announcement list is rebuilt by the off-on toggle below (measured: the toggle
        # re-announces an already-loaded OOPIF, and a same-site child — which never
        # becomes a target — announces nothing). DOM.performSearch is the gate: unlike
        # querySelectorAll it pierces closed shadow roots, and unlike Page.getFrameTree it
        # sees the host of an out-of-process child. On a trustworthy zero, the expensive
        # off/on re-announcement dance is skipped: one idempotent enable arms future
        # children and a short event window covers late SPA insertion. A truly frameless
        # page never pays the disable call or the longer multi-frame settle loop.
        # A handle that matched something is discarded at once; an empty one retains
        # nothing and is released in batches (`_discard_search`). When the probe fails or answers with
        # anything but a trustworthy non-negative integer, the dance runs anyway:
        # a frame report that says "none" must be earned, not assumed. Failing closed
        # here silently dropped OOPIFs on exactly the bot-walled pages this method
        # exists for.
        got: list[dict[str, Any]] = []

        def iframe_announcement(message: dict[str, Any]) -> bool:
            return (message.get("method") == "Target.attachedToTarget"
                    and ((message.get("params") or {}).get("targetInfo") or {}).get("type")
                    == "iframe")

        # Arm before the DOM probe. If auto-attach was already enabled, an OOPIF that
        # appears between the probe and the next command can otherwise announce itself in
        # that gap and be lost. On a fresh tab the one-way enable below re-announces any
        # child which won the same race.
        with self._armed(iframe_announcement) as w:
            search_id: str | None = None
            try:
                search = self.cdp(
                    "DOM.performSearch",
                    {"query": FRAME_HOST_QUERY, "includeUserAgentShadowDOM": True},
                    timeout=10.0,
                )
                raw_count = search.get("resultCount")
                search_id = (search.get("searchId")
                             if isinstance(search.get("searchId"), str) else None)
                count = (raw_count
                         if isinstance(raw_count, int) and not isinstance(raw_count, bool)
                         and raw_count >= 0 else None)
            except HarnessError:
                count = None                   # unknown → pay the dance, fail open
            finally:
                if search_id is not None:
                    self._discard_search(search_id, empty=count == 0)

            if count == 0:
                # Zero is evidence that no host existed at probe time, not that an SPA
                # will not insert one on its next task. Arm auto-attach once and wait on
                # Chrome's target event for a short bounded window. This avoids both a
                # guessed sleep and the expensive off/on re-announcement dance on the
                # common truly-frameless path.
                self.cdp("Target.setAutoAttach",
                         {"autoAttach": True, "waitForDebuggerOnStart": False,
                          "flatten": True}, timeout=10.0)
                announced = w.wait_match(lambda _m: True, FRAMES_ZERO_OBSERVE) is not None
            else:
                self.cdp("Target.setAutoAttach",
                         {"autoAttach": False, "waitForDebuggerOnStart": False,
                          "flatten": True}, timeout=10.0)
                self.cdp("Target.setAutoAttach",
                         {"autoAttach": True, "waitForDebuggerOnStart": False,
                          "flatten": True}, timeout=10.0)
                announced = True

            if announced:
                # Settle rather than sleep: stop as soon as a quiet window passes with no
                # new announcement. This is also a correctness fix. The old `wait_match(
                # lambda m: True, 0.6)` returned on the FIRST announcement and then read
                # `w.hits` immediately, so a page with several OOPIFs reported only the
                # ones that had happened to arrive by then — under-reporting frames,
                # silently.
                deadline = time.monotonic() + FRAMES_MAX_WAIT
                with w.cond:
                    counted = len(w.hits)
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    w.wait_match(lambda m: False, min(FRAMES_QUIET, left))
                    with w.cond:
                        n = len(w.hits)
                    if n == counted:
                        break                      # nothing new in a whole quiet window
                    counted = n
            seen: set[str] = set()
            for _, msg in w.hits:
                info = (msg.get("params") or {}).get("targetInfo") or {}
                target_id = str(info.get("targetId") or "")
                if target_id and target_id not in seen:
                    seen.add(target_id)
                    got.append({"target_id": target_id,
                                "url": info.get("url", ""), "kind": "oopif",
                                "reachable": "session.tab(target_id)"})
        out = got
        # Same-site iframes stay in the parent process and never become targets, so
        # getTargets alone reads as "no iframes" on a page that plainly has one.
        #
        # A trustworthy zero from the search above already answered this question, and it
        # answered it with more reach: `FRAME_HOST_QUERY` includes `iframe` and the search
        # pierces closed shadow roots, which `document.querySelectorAll` does not. So a
        # zero cannot be followed by a same-site iframe, and evaluating this would spend a
        # round trip to be told so. Every other case — a non-zero count, or a probe that
        # could not be trusted — still asks the document.
        try:
            same = [] if count == 0 else self._world_js(
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

    def _page_digest(self, *, max_chars: int, max_links: int,
                     content_only: bool, start: int = 0,
                     cursor: str | None = None,
                     semantic: bool = True) -> dict[str, Any]:
        """One main-world evaluation for the bounded page-reading surface."""
        max_chars = max(0, min(int(max_chars), 100_000))
        max_links = max(0, min(int(max_links), 500))
        start = max(0, int(start))
        source = (_SEMANTIC_DIGEST_JS
                  .replace("__MAX_CHARS__", str(max_chars))
                  .replace("__MAX_LINKS__", str(max_links))
                  .replace("__START__", str(start))
                  .replace("__CONTENT_ONLY__", "true" if content_only else "false"))
        value = self._main_js(source, timeout=15.0)
        raw = value if isinstance(value, dict) else {}
        if not semantic:
            return raw
        return self._semantic.render(raw, max_chars=max_chars, max_links=max_links,
                                     cursor=cursor, start=start)

    def read_page(self, max_chars: int = 6_000, max_links: int = 20, *,
                  content_only: bool = True, start: int = 0,
                  cursor: str | None = None) -> dict[str, Any]:
        """Versioned semantic blocks, links, metadata, and challenge signals.

        Continue with ``cursor`` to bind pagination to this exact document generation.
        ``start`` remains as a compatibility path for raw character offsets; unlike a block
        cursor, it cannot prove that the document stayed unchanged between calls.
        """
        with self._j.call("read_page", max_chars=max_chars, max_links=max_links,
                          content_only=content_only, start=start, cursor=bool(cursor)):
            return self._page_digest(max_chars=max_chars, max_links=max_links,
                                     content_only=content_only, start=start, cursor=cursor)

    def page_text(self, max_chars: int = 12_000, *, start: int = 0) -> str:
        """Rendered text, bounded and optionally paged with ``start``.

        `innerText` excludes script bodies and hidden nodes. The 12k default covers the
        large majority of reads while preventing one accidental full-page dump from
        dominating model context; callers can request a larger window explicitly.
        """
        with self._j.call("page_text", max_chars=max_chars, start=start):
            return str(self._page_digest(
                max_chars=max_chars, max_links=0, content_only=False, start=start,
                semantic=False,
            ).get("text") or "")

    def press_key(self, key: str, *, modifiers: int = 0,
                  timeout: float = 10.0) -> dict[str, Any]:
        """One named key. `text` is sent only for printable keys — attaching it to Enter or
        Tab makes Chrome insert a character instead of firing the shortcut (v1 paid for
        this with an uncleared field).

        Delivery is verified, not assumed: a renderer can drop key events while the
        dispatch ACKs anyway (measured on hidden tabs; the trigger is conditional and not
        fully isolated, so this checks rather than predicts). When the `__bh.keys`
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
        action_token = self._action_token()
        pre = _count(self._world_js(
            "(() => {const bh=window.__bh; if(!bh) return 0;"
            f" if(bh.beginAction) bh.beginAction({json.dumps(action_token)});"
            f" else bh.actionStarts[{json.dumps(action_token)}]=bh.mutations||0;"
            " return bh.keys||0;})()", timeout=timeout))
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
        synth = (_SYNTH_KEYS_JS.replace("__PRE__", json.dumps(pre))
                 .replace("__TEXT__", json.dumps(text))
                 .replace("__REF__", json.dumps(ref)))
        result = self._world_js(
            "await (async () => {const result = " + synth
            + "; await Promise.resolve(); return {result, consequence: "
            + self._action_consequence_source(action_token, refs=[ref] if ref else [])
            + "};})()", timeout=timeout)
        consequence_raw: Any = {}
        if isinstance(result, dict) and "result" in result:
            consequence_raw = result.get("consequence") or {}
            result = result.get("result")
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
        consequence = self._shape_action_consequence(consequence_raw)
        if not result.get("error") and (out["delivered"] or result.get("synthesized")):
            consequence.update(effect="input_delivery", verified=True)
        out["consequence"] = consequence
        if result.get("error"):
            out["error"] = result["error"]
            consequence.update(effect="input_failed", verified=False)
        return out

    def scroll(self, dy: int = 600, dx: int = 0, *, x: int = 400, y: int = 300,
               timeout: float = 10.0) -> dict[str, Any]:
        """Wheel event at a point, so it scrolls whatever container is under the cursor —
        an overflow pane, a virtualised list — not just the document.

        Verified like every other raw input: if no scroll event reached the document, the
        same container is scrolled through the DOM instead and `modality` reports which
        path ran. Unlike the conditional click/key drop, this one reproduced in every
        configuration tested — a non-selected tab never ACKs the wheel dispatch at all —
        and is filed upstream as browser-use/browser-harness#630, where the same call
        through v1's helpers raises TimeoutError and takes the whole script down.
        """
        with self._j.call("scroll", dy=dy, dx=dx):
            pre = _count(self._world_js("window.__bh ? __bh.scrolls : 0", timeout=timeout))
            try:
                # Short leash, deliberately: a hidden renderer does not merely drop the
                # wheel event — it never ACKs the dispatch at all (measured: 10s CDP
                # timeout in a background tab, reproduced on every build tried). The
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
                           max_dim: int | None = None, timeout: float = 20.0,
                           include_context: bool = False) -> dict[str, Any]:
        """JPEG by default (PNG when the path says so); output pixels == CSS viewport
        pixels on any display: `clip.scale = 1/devicePixelRatio` (item 21). `max_dim`
        lowers the scale further instead of resizing afterwards.

        `include_context` piggybacks the recording metadata (URL, title, focus box) on the
        viewport evaluation. A recorded frame therefore needs two CDP calls total instead
        of layout metrics + DPR + capture + a second context evaluation.
        """
        fmt = "png" if str(path or "").endswith(".png") else "jpeg"
        with self._j.call("screenshot", format=fmt) as span:
            viewport: dict[str, Any] = {}
            try:
                viewport = self._main_js("""(() => {
                  const e = document.activeElement;
                  const o = {x: window.scrollX || 0, y: window.scrollY || 0,
                    width: window.innerWidth || document.documentElement.clientWidth,
                    height: window.innerHeight || document.documentElement.clientHeight,
                    dpr: window.devicePixelRatio || 1,
                    u: location.href, t: document.title};
                  if (e && e !== document.body && e !== document.documentElement) {
                    const r = e.getBoundingClientRect();
                    if (r.width || r.height) o.box = [r.x, r.y, r.width, r.height];
                  }
                  return o;
                })()""", timeout=min(timeout, 5.0)) or {}
                if not isinstance(viewport, dict) or not viewport.get("width") \
                        or not viewport.get("height"):
                    viewport = {}
            except HarnessError:
                viewport = {}
            if viewport:
                x, y = float(viewport.get("x") or 0), float(viewport.get("y") or 0)
                cw, ch = int(viewport["width"]), int(viewport["height"])
                dpr = float(viewport.get("dpr") or 1)
            else:
                # Runtime can be unavailable while Page still answers. Preserve the old
                # screenshot path as a recovery fallback; DPR=1 is conservative and only
                # affects output density, never what region is captured.
                m = self.cdp("Page.getLayoutMetrics", timeout=timeout)
                css = m.get("cssLayoutViewport") or m["layoutViewport"]
                x, y = float(css.get("pageX", 0)), float(css.get("pageY", 0))
                cw, ch, dpr = int(css["clientWidth"]), int(css["clientHeight"]), 1.0
            scale = 1.0 / dpr
            if max_dim:
                scale = min(scale, max_dim / (max(cw, ch) * dpr))
            params: dict[str, Any] = {"format": fmt, "clip": {
                "x": x, "y": y,
                "width": cw, "height": ch, "scale": scale}}
            if fmt == "jpeg":
                params["quality"] = quality
            data = base64.b64decode(self.cdp("Page.captureScreenshot", params,
                                             timeout=timeout)["data"])
        if path:
            Path(path).write_bytes(data)
        out = {"path": str(path) if path else None, "bytes": len(data), "format": fmt,
               "css_viewport": [cw, ch], "scale": scale,
               # Recorder accounting reads the exact span counter; exposing it here avoids
               # another CDP query or a fragile scan back through the JSONL file.
               "cdp_calls": span.cdp_calls}
        if include_context and viewport:
            out["context"] = {key: viewport[key] for key in ("u", "t", "box")
                              if viewport.get(key) is not None}
        return out
