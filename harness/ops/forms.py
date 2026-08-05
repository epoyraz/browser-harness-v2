"""Form schema and one-pass fill (DESIGN.md D3/D15, TODO 23–25).

Everything here was forced by a real form in the five-ATS run:

  - **Proximity fallback.** On the Abacus/Umantis form the entire standard label chain
    (`label[for]` → wrapping label → aria → placeholder) resolves *nothing*: fields are
    named `customeraddressshoppervorname` and their labels are bare text in the table cell
    to the left. The fallback scores nearby text geometrically — same row to the left, or
    directly above.
  - **Placeholder-first options.** 7 of 8 selects opened with "Bitte wählen…"; a schema
    that does not mark them invites `options[0]` as an answer.
  - **Label-resolved selects.** A 249-option phone-prefix list is why `fill_form` takes a
    *label* and resolves it in-page: exact match, then prefix, then substring — and no
    match is `no_option_match` with candidates, never an index pick (v1 selected Spain).
  - **Furniture exclusion + form-identity verdict.** A 404 page passed a naive has-inputs
    check with 8 "fields": a cookie banner and a site search. The verdict exists so that
    page reads as NOT a form.
  - **One write per form.** The fill is a single evaluate: focus → set → input → change →
    blur per field, with a per-field `ok/got/want` report (rule 4 via `Tally`).
"""
from __future__ import annotations

import json
from typing import Any

from harness.core.outcome import Class, NotAForm, Outcome, Tally, fail, ok
from harness.ops.page import Tab

_SCHEMA_JS = """(() => {
  const bh = window.__bh || (window.__bh = {refs: {}, n: 0, mutations: 0});
  const furniture = el => !!el.closest(
    '[id*=cookie i],[class*=cookie i],[id*=consent i],[class*=consent i],' +
    '[id*=gdpr i],[class*=gdpr i],[role=search],nav,header aside');
  const labelFor = el => {
    if (el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l && l.innerText.trim()) return l.innerText.trim();
    }
    const wrap = el.closest('label');
    if (wrap) {
      const t = wrap.innerText.trim();
      if (t) return t;
    }
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = by.split(/\\s+/).map(i => (document.getElementById(i) || {}).innerText || '')
        .join(' ').trim();
      if (t) return t;
    }
    if (el.placeholder) return el.placeholder.trim();
    return null;
  };
  const nearText = el => {          // the Abacus fallback: geometry, not markup
    const r = el.getBoundingClientRect();
    let best = null, bestD = 1e9;
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (w.nextNode()) {
      const raw = w.currentNode.textContent.replace(/\\s+/g, ' ').trim();
      if (!raw || raw.length > 60) continue;
      const p = w.currentNode.parentElement;
      if (!p || p.closest('script,style,option,button,select')) continue;
      const pr = p.getBoundingClientRect();
      if (!pr.width && !pr.height) continue;
      const dx = r.left - pr.right, dy = r.top - pr.bottom;
      let d = null;
      if (pr.top < r.bottom && pr.bottom > r.top - 6 && dx >= -8 && dx < 260)
        d = dx + Math.abs(pr.top - r.top);                    // same row, to the left
      else if (dy >= -6 && dy < 48 && Math.abs(pr.left - r.left) < 160)
        d = dy + Math.abs(pr.left - r.left) * 0.5;            // directly above
      if (d !== null && d < bestD) { bestD = d; best = raw; }
    }
    return best;
  };
  const fields = [], files = [];
  const seen = new Set();
  const els = document.querySelectorAll(
    'input,select,textarea,[role=combobox],[contenteditable=true]');
  for (const el of els) {
    if (seen.has(el)) continue;
    seen.add(el);
    const tag = el.tagName.toLowerCase();
    const type = (el.type || '').toLowerCase();
    if (['submit', 'button', 'reset', 'image', 'hidden'].includes(type)) continue;
    const r = el.getBoundingClientRect();
    if (!r.width && !r.height) continue;
    if (furniture(el) || type === 'search') continue;
    if (type === 'file') { files.push(el.name || el.id || 'file'); continue; }
    let ref = el.__bhRef;
    if (!ref || bh.refs[ref] !== el) { ref = 'e' + (++bh.n); el.__bhRef = ref; bh.refs[ref] = el; }
    const label = labelFor(el) || nearText(el);
    const kind = el.getAttribute('role') === 'combobox' && tag !== 'select' ? 'combobox'
      : el.isContentEditable && tag !== 'input' && tag !== 'textarea' ? 'richtext'
      : tag === 'select' ? 'select' : tag === 'textarea' ? 'textarea' : (type || 'text');
    const f = {ref, kind, label,
               name: el.name || el.id || null,
               required: !!(el.required || el.getAttribute('aria-required') === 'true'
                            || (label && /\\*\\s*$/.test(label)))};
    const auto = el.getAttribute('autocomplete');
    if (auto && auto !== 'off') f.autocomplete = auto;
    if (tag === 'select') {
      const opts = [...el.options];
      f.options_count = opts.length;
      f.options_sample = opts.slice(0, 8).map(o => o.text.trim().slice(0, 40));
      const first = opts[0];
      f.placeholder_first = !!first && (first.value === '' || first.disabled
        || /^(bitte|please|select|choose|choisir|--|\\.\\.\\.|w\\u00e4hlen)/i
           .test(first.text.trim()));
    }
    if (kind === 'combobox') f.needs_interaction = true;   // invisible to v1 entirely
    if (type === 'checkbox' || type === 'radio') f.checked = el.checked;
    else if (el.value) f.value = String(el.value).slice(0, 60);
    fields.push(f);
  }
  const submits = [...document.querySelectorAll(
    'button[type=submit],input[type=submit],button:not([type])')]
    .filter(b => !furniture(b))
    .map(b => (b.innerText || b.value || '').trim()).filter(Boolean);
  const verdict = {
    is_form: fields.length >= 2 && submits.length > 0,
    reason: fields.length < 2 ? 'fewer than 2 real fields after furniture exclusion'
      : submits.length === 0 ? 'no submit control' : 'fields plus a submit control',
    fields: fields.length,
    required: fields.filter(f => f.required).length,
    files: files.length,
    submit_labels: submits.slice(0, 5)};
  return {verdict, fields, files};
})()"""

_FILL_JS = """((plan) => {
  const bh = window.__bh || {refs: {}};
  const report = [];
  const nativeSet = (el, v) => {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype
                                            : HTMLInputElement.prototype;
    const d = Object.getOwnPropertyDescriptor(proto, 'value');
    if (d && d.set) d.set.call(el, v); else el.value = v;   // native setter: React et al.
  };
  const fire = (el, types) => types.forEach(t => el.dispatchEvent(
    t === 'input' ? new InputEvent('input', {bubbles: true})
                  : new Event(t, {bubbles: true})));
  const norm = s => String(s).replace(/\\s+/g, ' ').trim().toLowerCase();
  for (const step of plan) {
    const el = bh.refs[step.ref];
    if (!el) { report.push({ref: step.ref, ok: false, error: 'element_gone'}); continue; }
    try {
      el.focus();
      if (el.tagName === 'SELECT') {
        const want = norm(step.label ?? step.value ?? '');
        const opts = [...el.options];
        const hit = opts.find(o => norm(o.text) === want || norm(o.value) === want)
                 || opts.find(o => norm(o.text).startsWith(want))
                 || opts.find(o => norm(o.text).includes(want));
        if (!hit) {
          report.push({ref: step.ref, ok: false, error: 'no_option_match',
                       want: String(step.label ?? step.value ?? ''),
                       candidates: opts.slice(0, 8).map(o => o.text.trim().slice(0, 40))});
          continue;
        }
        el.value = hit.value;
        fire(el, ['input', 'change', 'blur']);
        report.push({ref: step.ref, ok: el.value === hit.value,
                     want: String(step.label ?? step.value ?? ''),
                     got: el.selectedOptions[0] ? el.selectedOptions[0].text.trim() : el.value});
      } else if (el.type === 'checkbox' || el.type === 'radio') {
        el.checked = !!step.value;
        fire(el, ['input', 'change', 'blur']);
        report.push({ref: step.ref, ok: el.checked === !!step.value,
                     want: !!step.value, got: el.checked});
      } else {
        const v = String(step.value ?? '');
        nativeSet(el, v);
        fire(el, ['input', 'change', 'blur']);
        report.push({ref: step.ref, ok: el.value === v,
                     want: v.slice(0, 80), got: String(el.value).slice(0, 80)});
      }
    } catch (e) {
      report.push({ref: step.ref, ok: false, error: String(e).slice(0, 120)});
    }
  }
  return report;
})(__PLAN__)"""

_STEP_CLASS = {"element_gone": Class.ELEMENT_GONE, "no_option_match": Class.NO_OPTION_MATCH}


def form_schema(tab: Tab, *, timeout: float = 20.0) -> dict[str, Any]:
    """One evaluate: `{verdict, fields, files}`. Fields carry refs from the shared
    registry, so a schema field is directly fillable and clickable."""
    with tab.journal.call("form_schema"):
        return tab.js(_SCHEMA_JS, timeout=timeout)


def require_form(schema: dict[str, Any]) -> dict[str, Any]:
    """Raise `NotAForm` when the verdict says so — the 404-page guard, made explicit."""
    verdict = schema.get("verdict") or {}
    if not verdict.get("is_form"):
        raise NotAForm(verdict.get("reason", "no form identified"), **verdict)
    return schema


def fill_form(tab: Tab, plan: list[dict[str, Any]], *, timeout: float = 30.0) -> Outcome:
    """One write for the whole plan. Rule 4: OK only when every field verified; PARTIAL
    carries the full per-field report either way (`outcome.value`)."""
    if not plan:
        return ok([], attempted=0, succeeded=0, failed=0)
    src = _FILL_JS.replace("__PLAN__", json.dumps(plan))
    with tab.journal.call("fill_form", n=len(plan)):
        report = tab.js(src, timeout=timeout) or []
    tally = Tally()
    for i, step in enumerate(plan):
        r = report[i] if i < len(report) else {"ref": step.get("ref"), "ok": False,
                                               "error": "no report entry"}
        if r.get("ok"):
            tally.record(ok(r))
        else:
            cls = _STEP_CLASS.get(r.get("error", ""), Class.JS_EXCEPTION)
            tally.record(fail(cls, r.get("error", ""), **r))
    return tally.outcome(value=report, fields=len(plan))     # value = the FULL report


def set_value(tab: Tab, ref: str, value: Any, *, keystrokes: bool = False,
              timeout: float = 20.0) -> Outcome:
    """One field, one round trip (D3). `keystrokes=True` opts into real input via
    `Input.insertText` — the whole string in ONE command, for editors that ignore
    synthetic events. v1's per-character dispatch made a 20-char fill cost 61 round
    trips; a 2,000-char paste here is one call either way."""
    if not keystrokes:
        return fill_form(tab, [{"ref": ref, "value": value}], timeout=timeout)
    focused = tab.js(
        f"(() => {{const el = window.__bh && __bh.refs[{json.dumps(ref)}]; if (!el) return false;"
        " el.focus(); el.select && el.select(); return true;})()",
        timeout=timeout)
    if not focused:
        return fail(Class.ELEMENT_GONE, f"no element registered for ref {ref!r}", ref=ref)
    tab.cdp("Input.insertText", {"text": str(value)}, timeout=timeout)
    got = tab.js(f"(() => {{const el = __bh.refs[{json.dumps(ref)}]; el.blur();"
                 " return String(el.value).slice(0, 80);})()",
                 timeout=timeout)
    want = str(value)
    if got == want[:80] or got == want:
        return ok({"ref": ref, "got": got}, mode="keystrokes")
    return fail(Class.JS_EXCEPTION, "value did not stick", ref=ref,
                want=want[:80], got=got)
