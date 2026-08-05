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
import time
from typing import Any

from harness.core.outcome import Class, NotAForm, Outcome, Tally, fail, ok
from harness.ops.page import Tab


def _digits(s: str) -> str:
    """Compare phone-ish values by their digits: a control that reformats
    `+41791234567` to `+41 79 123 45 67` accepted the value, it did not reject it."""
    return "".join(c for c in str(s) if c.isdigit())

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
    const cs = getComputedStyle(el);
    // A control a human cannot see is a decoy, not a field. Select2 puts a 1x1
    // clip-rect(0,0,0,0) "focusser" input where the real <select> was: writing to it
    // sticks, submits nothing, and reads back as success — a false positive, which is
    // worse than a failure. The real control is picked up by the hidden-select rule below.
    const clipped = cs.clip === 'rect(0px, 0px, 0px, 0px)' || cs.clipPath === 'inset(50%)';
    const decoy = clipped || (r.width <= 2 && r.height <= 2);
    // ...but a hidden <select> IS the form's data, so keep it and say it is hidden.
    const hiddenControl = tag === 'select' && (decoy || (!r.width && !r.height));
    if (decoy && !hiddenControl) continue;
    if (!r.width && !r.height && !hiddenControl) continue;
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
    if (hiddenControl) {
      // Fillable (it is the real form control) but the widget painted over it will not
      // redraw, so a human looking at the page still sees the old label.
      f.hidden_control = true;
      f.widget = !!el.closest('.select2-container, .chosen-container, [data-widget]')
        || !!(el.parentElement
              && el.parentElement.querySelector('.select2-container, .chosen-container'));
    }
    if (type === 'checkbox' || type === 'radio') f.checked = el.checked;
    else if (el.value) f.value = String(el.value).slice(0, 60);
    fields.push(f);
  }
  const submits = [...document.querySelectorAll(
    'button[type=submit],input[type=submit],input[type=button],button:not([type])')]
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
      // A widget with no value property cannot be set at all. jobs.ch's phone-country
      // control is a DIV[role=combobox]; calling HTMLInputElement's value setter on it
      // throws "Illegal invocation". form_schema already flags these needs_interaction —
      // refusing here with a typed error keeps the fill honest instead of crashing.
      const settable = el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement
                    || el instanceof HTMLSelectElement;
      if (!settable) {
        if (el.isContentEditable) {                 // rich-text: value is meaningless
          el.textContent = String(step.value ?? '');
          fire(el, ['input', 'change', 'blur']);
          report.push({ref: step.ref, ok: el.textContent === String(step.value ?? ''),
                       want: String(step.value ?? '').slice(0, 80),
                       got: String(el.textContent).slice(0, 80)});
        } else {
          report.push({ref: step.ref, ok: false, error: 'needs_interaction',
                       tag: el.tagName.toLowerCase(),
                       role: el.getAttribute('role') || null,
                       want: String(step.label ?? step.value ?? '')});
        }
        continue;
      }
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

_STEP_CLASS = {"element_gone": Class.ELEMENT_GONE, "no_option_match": Class.NO_OPTION_MATCH,
               "needs_interaction": Class.NEEDS_INTERACTION}


def form_schema(tab: Tab, *, timeout: float = 20.0) -> dict[str, Any]:
    """One evaluate: `{verdict, fields, files}`. Fields carry refs from the shared
    registry, so a schema field is directly fillable and clickable."""
    with tab.journal.call("form_schema"):
        return tab._world_js(_SCHEMA_JS, timeout=timeout)


def require_form(schema: dict[str, Any]) -> dict[str, Any]:
    """Raise `NotAForm` when the verdict says so — the 404-page guard, made explicit."""
    verdict = schema.get("verdict") or {}
    if not verdict.get("is_form"):
        raise NotAForm(verdict.get("reason", "no form identified"), **verdict)
    return schema


_RECHECK_JS = """((plan) => {
  const bh = window.__bh || {refs: {}};
  return plan.map(step => {
    const el = bh.refs[step.ref];
    if (!el) return null;
    if (el.tagName === 'SELECT')
      return el.selectedOptions[0] ? el.selectedOptions[0].text.trim() : String(el.value);
    if (el.type === 'checkbox' || el.type === 'radio') return el.checked;
    if (!(el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement))
      return el.isContentEditable ? String(el.textContent) : null;
    return String(el.value);
  });
})(__PLAN__)"""


def fill_form(tab: Tab, plan: list[dict[str, Any]], *, timeout: float = 30.0,
              recheck: float = 0.15) -> Outcome:
    """One write for the whole plan. Rule 4: OK only when every field verified; PARTIAL
    carries the full per-field report either way (`outcome.value`).

    `recheck` re-reads every field after a settle, in **one** extra evaluate, because the
    immediate `el.value === want` check is measured too early for framework-controlled
    inputs. Measured on jobs.ch: a React phone field rejects `079 123 45 67` outright but
    rewrites `+41791234567` to `+41 79 123 45 67` — so the write succeeded while the
    immediate check said it failed, and a normalising field would otherwise be reported as
    a failure forever. Set `recheck=0` to skip the settle when speed matters more.
    """
    if not plan:
        return ok([], attempted=0, succeeded=0, failed=0)
    src = _FILL_JS.replace("__PLAN__", json.dumps(plan))
    with tab.journal.call("fill_form", n=len(plan)):
        report = tab._world_js(src, timeout=timeout) or []
        if recheck > 0:
            time.sleep(recheck)
            settled = tab._world_js(_RECHECK_JS.replace("__PLAN__", json.dumps(plan)),
                                    timeout=timeout) or []
            for i, entry in enumerate(report):
                if i >= len(settled) or settled[i] is None or "error" in entry:
                    continue
                entry["settled"] = settled[i]
                if not entry.get("ok"):
                    # rule 3: success is "the field now holds a value the page accepted",
                    # not "the string came back byte-identical".
                    want = str(entry.get("want", ""))
                    got = "" if settled[i] is None else str(settled[i])
                    if got and (got == want or _digits(got) == _digits(want)):
                        entry["ok"] = True
                        entry["normalized"] = True
                        entry["got"] = got
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


def set_value(tab: Tab, ref: str, value: Any, *, mode: str = "value",
              keystrokes: bool | None = None, timeout: float = 20.0,
              recheck: float = 0.15) -> Outcome:
    """One field. Three tiers, because measurement showed two were not enough (D3).

    | mode      | round trips | `isTrusted` | per-key handlers | use when |
    |-----------|-------------|-------------|------------------|----------|
    | `value`   | 1           | **false**   | no               | the default; most fields |
    | `insert`  | 2           | true        | no               | the page checks `isTrusted` |
    | `type`    | ~3N         | true        | **yes**          | typeahead / incremental mask |

    Measured on a page instrumented to count them: a one-shot write opened a keystroke
    typeahead **0 times** and left its dropdown empty; `Input.insertText` also opened it
    **0 times** despite producing trusted events; only per-character `dispatchKeyEvent`
    opened it (5 times) and populated the dropdown. So `insert` does *not* subsume `type` —
    which is exactly why v1 types character by character, and why deleting that capability
    rather than demoting it would have been a regression.

    `type` is the slow tier on purpose: it is v1's cost model, kept for the cases that
    genuinely need it rather than paid by default on every field.
    """
    if keystrokes is not None:                    # back-compat for the bool spelling
        mode = "insert" if keystrokes else "value"
    if mode not in ("value", "insert", "type"):
        raise ValueError(f"mode must be value|insert|type, got {mode!r}")
    if mode == "value":
        return fill_form(tab, [{"ref": ref, "value": value}], timeout=timeout,
                         recheck=recheck)
    focused = tab._world_js(
        f"(() => {{const el = window.__bh && __bh.refs[{json.dumps(ref)}]; if (!el) return false;"
        " el.focus(); el.select && el.select(); return true;})()",
        timeout=timeout)
    if not focused:
        return fail(Class.ELEMENT_GONE, f"no element registered for ref {ref!r}", ref=ref)
    if mode == "insert":
        tab.cdp("Input.insertText", {"text": str(value)}, timeout=timeout)
    else:
        for ch in str(value):
            # keyDown carrying `text` is what makes the page see a real character; the
            # matching keyUp is what a keystroke-driven typeahead listens for.
            tab.cdp("Input.dispatchKeyEvent", {"type": "keyDown", "text": ch,
                                               "key": ch, "unmodifiedText": ch},
                    timeout=timeout)
            tab.cdp("Input.dispatchKeyEvent", {"type": "keyUp", "key": ch}, timeout=timeout)
    got = tab._world_js(f"(() => {{const el = __bh.refs[{json.dumps(ref)}]; el.blur();"
                 " return String(el.value).slice(0, 80);})()",
                 timeout=timeout)
    want = str(value)
    if got == want[:80] or got == want or _digits(got) == _digits(want) != "":
        return ok({"ref": ref, "got": got}, mode=mode)
    return fail(Class.JS_EXCEPTION, "value did not stick", ref=ref, mode=mode,
                want=want[:80], got=got)
