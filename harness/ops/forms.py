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
import re
import subprocess
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from harness.core.outcome import Class, NotAForm, Outcome, Tally, fail, ok
from harness.ops.page import Tab

# ATS route conventions belong in one capability registry.  They are not form selectors:
# they produce reversible candidate GET routes which the normal form guard still verifies.
_APPLICATION_ROUTE_RULES = (
    (re.compile(r"^jobs\.ashbyhq\.com$", re.IGNORECASE),
     re.compile(r"^/[^/]+/[0-9a-f-]{20,}/?$", re.IGNORECASE), "application"),
)


def application_route_candidates(url: str) -> list[str]:
    """Return known direct application views for a posting URL, in preference order."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return []
    for host, path, suffix in _APPLICATION_ROUTE_RULES:
        if host.fullmatch(parts.hostname or "") and path.fullmatch(parts.path):
            candidate = urlunsplit((parts.scheme, parts.netloc,
                                    f"{parts.path.rstrip('/')}/{suffix}",
                                    parts.query, ""))
            return [candidate]
    return []


def _digits(s: str) -> str:
    """Compare phone-ish values by their digits: a control that reformats
    `+41791234567` to `+41 79 123 45 67` accepted the value, it did not reject it."""
    return "".join(c for c in str(s) if c.isdigit())

_SCHEMA_JS = """(() => {
  const bh = window.__bh;
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
  const fields = [], files = [], fieldNodes = [], fileNodes = [];
  const seen = new Set();
  const els = document.querySelectorAll(
    'input,select,textarea,[role=combobox],[contenteditable=true]');
  for (const el of els) {
    if (seen.has(el)) continue;
    seen.add(el);
    const tag = el.tagName.toLowerCase();
    const type = (el.type || '').toLowerCase();
    if (['submit', 'button', 'reset', 'image', 'hidden'].includes(type)) continue;
    // Styled upload controls are often visually replaced by a button/label.  They are
    // still decisive application evidence even when their own box is clipped.
    if (type === 'file') {
      if (!bh.furniture(el)) {
        files.push(el.name || el.id || 'file');
        fileNodes.push(el);
      }
      continue;
    }
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
    if (bh.furniture(el) || type === 'search') continue;
    const ref = bh.ref(el);
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
    fieldNodes.push(el);
  }
  const submitNodes = [...document.querySelectorAll(
    'button[type=submit],input[type=submit],input[type=button],button:not([type])')]
    .filter(b => !bh.furniture(b));
  const submits = submitNodes.map(b => (b.innerText || b.value || '').trim()).filter(Boolean);
  // "fewer than 2 real fields" is true of a bot wall, an unbooted SPA and a form whose
  // controls are all hidden — three different problems with three different fixes. Say
  // which one it is, and carry the counts that prove it.
  const textLen = ((document.body && document.body.innerText) || '').trim().length;
  const controls = [...document.querySelectorAll('input,textarea,select')];
  const inDom = controls.length;
  const visible = controls.filter(e => e.offsetParent !== null).length;
  // "0 visible" is where diagnosis used to stop, and it stopped one step short of the
  // fix every time. A form collapsed behind an apply button, a page of tracking pixels
  // and an app that rendered nothing all report 0 — with three different remedies.
  // Attributing each hidden control to a cause costs nothing (the nodes are already
  // walked) and names the remedy: `hidden_ancestor` in bulk means the form is behind a
  // step, so look for a control to click; `self_display_none` means tracking furniture,
  // so ignore it. Measured live on Recruitee: 92 in the DOM, 0 visible, and the cause
  // went uninvestigated across five runs because the verdict never said which one.
  const why = {};
  for (const el of controls) {
    if (el.offsetParent !== null) continue;
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    let cause;
    if (cs.display === 'none') cause = 'self_display_none';
    else if (cs.visibility === 'hidden' || cs.visibility === 'collapse') cause = 'self_visibility_hidden';
    else if (parseFloat(cs.opacity) === 0) cause = 'self_opacity_zero';
    else if (cs.position === 'fixed') cause = 'position_fixed';
    else if (!r.width && !r.height) cause = 'hidden_ancestor';
    else if (r.right < 0 || r.bottom < 0
             || r.left > (document.documentElement.clientWidth || 0)) cause = 'offscreen';
    else cause = 'zero_rect';
    why[cause] = (why[cause] || 0) + 1;
  }
  const ownerOf = el => el.form || (el.closest && el.closest('form')) || document;
  const groups = new Map();
  const groupFor = el => {
    const owner = ownerOf(el);
    if (!groups.has(owner)) groups.set(owner, {owner, fields: [], files: [], submits: []});
    return groups.get(owner);
  };
  fieldNodes.forEach((el, index) => groupFor(el).fields.push({el, field: fields[index]}));
  fileNodes.forEach(el => groupFor(el).files.push(el));
  submitNodes.forEach(el => groupFor(el).submits.push(el));

  const APP = /(?:apply|application|bewerb|postul|candidat|candidature)/i;
  const AUTH = /(?:sign\\s*in|log\\s*in|login|anmelden|connexion|create\\s+(?:an\\s+)?account|konto\\s+erstellen|register|registrieren)/i;
  const ACCOUNT = /(?:login|account|konto|benutzer(?:name|profil)?|register|registr|user\\s+profile|profil\\s+angelegt|create\\s+(?:a\\s+)?profile)/i;
  const NEWSLETTER = /(?:newsletter|job.?alert|subscribe|abonnieren|abonnement|apply\\s+later)/i;
  const IDENTITY = /(?:e-?mail|courriel|user(?:name)?|login|benutzer)/i;
  const classifications = [];
  for (const group of groups.values()) {
    const owner = group.owner;
    const ownerText = owner === document ? '' : String(owner.innerText || '').slice(0, 5000);
    const fieldEvidence = group.fields.map(({el, field}) =>
      [el.id, el.name, field.label, field.autocomplete].filter(Boolean).join(' ')).join(' ');
    const submitEvidence = group.submits.map(el =>
      [el.id, el.name, el.innerText, el.value, el.getAttribute('aria-label')]
        .filter(Boolean).join(' ')).join(' ');
    const structural = [location.href, document.title,
      owner === document ? '' : owner.action, owner === document ? '' : owner.id,
      owner === document ? '' : owner.getAttribute('name'), fieldEvidence, submitEvidence]
      .filter(Boolean).join(' ');
    const identities = group.fields.filter(({el, field}) =>
      (el.type || '').toLowerCase() === 'email'
      || ['email', 'username'].includes(String(field.autocomplete || '').toLowerCase())
      || IDENTITY.test([el.id, el.name, field.label].filter(Boolean).join(' ')));
    const passwords = group.fields.filter(({el}) => (el.type || '').toLowerCase() === 'password');
    const applicationSemantic = APP.test(structural);
    // A file input is decisive evidence even when its own box is clipped — styled upload
    // controls hide the real input behind a label — but ONLY on a page whose form is
    // actually open. A collapsed application (display:none panel behind an Apply button)
    // contains the same file input, and counting it classified four Recruitee POSTINGS
    // as application_form: the caller then stopped one click short of the real form and
    // filled nothing. Visible sibling fields are what separate the two cases — an open
    // form shows its fields even when its upload control is a styled label; a collapsed
    // one shows neither.
    const visibleFiles = group.files.filter(el => el.offsetParent !== null).length;
    const applicationStructure = (group.files.length > 0
        && (group.fields.length >= 1 || visibleFiles > 0))
      || (group.fields.length >= 3 && applicationSemantic);
    const accountSemantic = ACCOUNT.test(ownerText + ' ' + fieldEvidence + ' ' + structural);
    const authSemantic = AUTH.test(ownerText + ' ' + structural);
    const newsletter = NEWSLETTER.test(ownerText + ' ' + structural);
    let classification = 'not_form';
    if (applicationStructure && identities.length && (passwords.length || accountSemantic))
      classification = 'application_form_with_account_fields';
    else if (applicationStructure)
      classification = 'application_form';
    else if (group.fields.length <= 3 && identities.length && passwords.length
             && group.submits.length)
      classification = 'login_email_password';
    else if (group.fields.length <= 2 && identities.length && authSemantic && !newsletter)
      classification = 'login_email_first';
    else if (group.fields.length >= 2 && group.submits.length)
      classification = 'generic_form';
    classifications.push({
      classification,
      fields: group.fields.length,
      files: group.files.length,
      submits: group.submits.length,
      identity_fields: identities.length,
      password_fields: passwords.length,
      application_semantic: applicationSemantic,
      auth_semantic: authSemantic,
    });
  }
  const priority = [
    'application_form_with_account_fields', 'application_form',
    'login_email_password', 'login_email_first', 'generic_form', 'not_form'];
  const classification = priority.find(kind =>
    classifications.some(item => item.classification === kind)) || 'not_form';
  const isApplication = classification === 'application_form'
    || classification === 'application_form_with_account_fields';
  const isGenericForm = classification === 'generic_form';
  const isAuthentication = classification === 'login_email_password'
    || classification === 'login_email_first';
  const isForm = classification !== 'not_form';
  let reason;
  if (classification === 'application_form_with_account_fields')
    reason = 'application structure with embedded account fields';
  else if (classification === 'application_form')
    reason = 'application structure';
  else if (classification === 'login_email_password')
    reason = 'standalone email/username and password authentication form';
  else if (classification === 'login_email_first')
    reason = 'standalone email-first or passwordless authentication form';
  else if (isGenericForm)
    reason = 'generic form without application evidence';
  else if (textLen === 0 && inDom === 0)
    reason = 'page rendered nothing: 0 characters and 0 form controls. The document is '
           + 'empty — a bot wall, or an app whose boot request never completed';
  else if (fields.length < 2 && inDom >= 2 && visible === 0) {
    const top = Object.entries(why).sort((a, b) => b[1] - a[1])[0];
    reason = 'form controls exist but none are usable: ' + inDom + ' in the DOM, 0 visible'
           + (top ? ' — ' + top[1] + ' of them ' + top[0].replace(/_/g, ' ') : '')
           + '; the form is collapsed or behind another step';
  }
  else if (fields.length < 2)
    reason = 'fewer than 2 real fields after furniture exclusion';
  else reason = 'no submit control';
  const verdict = {
    is_form: isForm,
    is_application: isApplication,
    is_generic_form: isGenericForm,
    is_authentication: isAuthentication,
    classification,
    reason,
    fields: fields.length,
    required: fields.filter(f => f.required).length,
    files: files.length,
    controls_in_dom: inDom,
    controls_visible: visible,
    invisible_because: why,
    text_len: textLen,
    submit_labels: submits.slice(0, 5),
    form_classifications: classifications};
  return {verdict, fields, files};
})()"""

_PREPARE_JS = """(() => {
  const schema = __SCHEMA__;
  const bh = window.__bh;
  const fileInputs = [...document.querySelectorAll('input[type=file]')].map(el => {
    const ref = bh.ref(el);
    const label = el.id ? document.querySelector('label[for="' + CSS.escape(el.id) + '"]') : null;
    return {ref, name: el.name || el.id || 'file',
            label: (label && label.innerText || el.getAttribute('aria-label') || '').trim(),
            accept: el.accept || '', multiple: !!el.multiple};
  });
  // The verb is rarely the first word. "Auf diese Stelle bewerben" is the commonest German
  // apply label there is, and an anchored pattern can never match it — measured 1 hit in 34
  // real postings. Match the verb ANYWHERE, in the languages Swiss ATSs actually ship, and
  // score rather than take-first so a nav item reading "Apply" cannot beat the real link.
  const APPLY_TEXT =
    /(bewerb|apply|postul|candidat|candida|sollicit|solicit|aplicar|ansök|søk|(?:déposer|deposer|envoyer|soumettre)\\s+(?:(?:ma|une|la)\\s+)?(?:demande|dossier)|(?:presenta|invia|manda)\\s+(?:(?:la|una)\\s+)?domanda)/i;
  const APPLY_HREF = /(apply|application|bewerb|postul|candidat|domanda)/i;
  // "Apply filters", "Bewerbungstipps", a privacy link — same verb, wrong destination.
  const NOT_APPLY =
    /(filter|tipp|tips|ratgeber|guide|faq|hilfe|help|datenschutz|privacy|impressum|cookie|newsletter|alert|job.?alert|abo)/i;
  const labelOf = el => (el.innerText || el.value || el.getAttribute('aria-label')
                         || el.title || '').replace(/\\s+/g, ' ').trim();
  //: Shared by both kinds. The label must carry the verb: scoring on href alone found
  //: nothing the text did not already find, and did produce a false positive — a
  //: "Candidates Privacy Notice" link whose href happened to contain /apply/. href is a
  //: tie-break, not evidence.
  const scoreLabel = txt => {
    if (!txt || txt.length > 60 || NOT_APPLY.test(txt)) return -1;
    if (!APPLY_TEXT.test(txt)) return -1;
    return txt.length <= 30 ? 4 : 3;          // a button label, not a sentence
  };
  let apply = null, bestScore = 0;
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.getAttribute('href') || '';
    if (!href || href.startsWith('#') || /^(javascript|mailto|tel):/i.test(href)) continue;
    let s = scoreLabel(labelOf(a));
    if (s < 0) continue;
    if (APPLY_HREF.test(href)) s += 2;
    const r = a.getBoundingClientRect();
    if (r.width && r.height) s += 1;          // a visible control beats a hidden one
    if (s > bestScore) { bestScore = s; apply = a; }
  }
  // An <a href> is not the only way to offer an application, and on two ATSs it is not
  // the way at all: Recruitee and BambooHR render a <button> that expands the form in
  // place. There is no URL to navigate to, so a matcher that scores only anchors returns
  // null and the caller has nowhere to go — measured 0/4 filled on Recruitee in all five
  // live runs, while the schema simultaneously reported ~92 controls sitting in the DOM,
  // invisible, waiting for that button. So return a clickable ref alongside the link.
  let ctl = null, ctlScore = 0, ctlPainted = true;
  for (const b of document.querySelectorAll(
       'button, [role=button], summary, input[type=button], input[type=submit]')) {
    // A submit inside a form that is already showing is the send button, not the way in.
    if (b.form && b.type === 'submit') continue;
    let s = scoreLabel(labelOf(b));
    if (s < 0) continue;
    const r = b.getBoundingClientRect();
    // A laid-out control is strongly preferred, but an unpainted one is NOT discarded.
    // "No box" conflates two different things: a control the page is deliberately
    // hiding, and one an SPA has simply not painted yet. Measured on an Oracle careers
    // site: at prepare time the document had 0 visible controls and the real
    // "POSTULER MAINTENANT" button had no box; a DOM click on it navigated to /apply
    // and produced the form. Discarding it returned apply_control=null and the run
    // stopped at "usable_ui" with the way in sitting right there. So an unpainted
    // candidate is kept at a lower score — the caller's click already falls back
    // through the DOM when the compositor path is inert, which is exactly this case.
    const painted = !!(r.width && r.height);
    if (!painted) s -= 2;
    if (b.tagName === 'SUMMARY') s += 1;      // <details> is literally "there is more"
    if (s > ctlScore) { ctlScore = s; ctl = b; ctlPainted = painted; }
  }
  let applyControl = null;
  if (ctl) {
    const ref = bh.ref(ctl);
    applyControl = {ref, label: labelOf(ctl).slice(0, 60), score: ctlScore,
                    tag: ctl.tagName.toLowerCase(), painted: ctlPainted};
  }
  // Read-only structured-data tier. SPA shells often carry their routes in JSON-LD,
  // __NEXT_DATA__, or another application/json bootstrap even when no clickable control
  // has rendered yet. Walk bounded JSON and return only URL-shaped application routes;
  // never return the JSON body itself, which may contain unrelated page data.
  const applicationUrls = [];
  const seenObjects = new Set(); let visited = 0;
  const route = value => {
    if (typeof value !== 'string' || value.length > 2000
        || !/(apply|application|bewerb|postul|candidat|domanda)/i.test(value)) return;
    const raw = value.trim();
    // `new URL(any text, base)` happily turns a job-description HTML string into a
    // percent-encoded relative URL.  Only pass URL-shaped strings: absolute/root/dot
    // paths, an application-named leaf, or a conventional multi-segment relative path.
    // Reject markup, quotes and whitespace before URL parsing.
    if (!raw || /[<>"'\\s]/.test(raw)) return;
    const urlShaped = /^(?:https?:\\/\\/|\\/\\/|\\/|\\.{1,2}\\/)/i.test(raw)
      || /^(?:apply|application|bewerb|postul|candidat|domanda)(?:[/?#]|$)/i.test(raw)
      || /^[A-Za-z0-9._~%+-]+(?:\\/[A-Za-z0-9._~!$&()*+,;=:@%?#-]*)+$/.test(raw);
    if (!urlShaped) return;
    try {
      const url = new URL(raw, location.href);
      if (/^https?:$/.test(url.protocol) && !applicationUrls.includes(url.href))
        applicationUrls.push(url.href);
    } catch (e) {}
  };
  const walk = value => {
    if (visited++ > 5000 || value == null) return;
    if (typeof value === 'string') { route(value); return; }
    if (typeof value !== 'object' || seenObjects.has(value)) return;
    seenObjects.add(value);
    for (const child of Array.isArray(value) ? value : Object.values(value)) walk(child);
  };
  for (const script of document.querySelectorAll(
       'script[type="application/ld+json"],script[type="application/json"],script#__NEXT_DATA__')) {
    try { walk(JSON.parse(script.textContent || 'null')); } catch (e) {}
  }
  return {schema, url: location.href, title: document.title,
          language: (document.documentElement && document.documentElement.lang)
                    || navigator.language || 'en',
          file_inputs: fileInputs, apply_link: apply ? apply.href : null,
          apply_control: applyControl, application_urls: applicationUrls.slice(0, 12)};
})()""".replace("__SCHEMA__", _SCHEMA_JS)


def prepare_document(tab: Tab, *, guard_submit: bool = True,
                     timeout: float = 20.0) -> dict[str, Any]:
    """Return all application perception data in one round trip.

    ``guard_submit`` remains source-compatible, but cannot disable the session's automatic
    dry-run boundary. Every document is guarded before page script in ``Tab``.
    """
    return tab._world_js(_PREPARE_JS, timeout=timeout)

_FILL_JS = """((plan) => {
  const bh = window.__bh || {refs: {}};
  bh.actionStarts = bh.actionStarts || {};
  if (bh.beginAction) bh.beginAction(__ACTION_TOKEN__);
  else bh.actionStarts[__ACTION_TOKEN__] = bh.mutations || 0;
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
        const requested = Array.isArray(step.labels) ? step.labels
                        : [step.label ?? step.value ?? ''];
        const wants = requested.map(norm).filter(Boolean);
        const opts = [...el.options];
        // Ordered candidate lists are exact-only.  They express semantic equivalence,
        // not permission to choose a vaguely similar option.  The legacy single-label
        // spelling keeps its starts/includes fallback for compatibility.
        const exact = want => opts.find(o => norm(o.text) === want || norm(o.value) === want);
        let hit = wants.map(exact).find(Boolean);
        if (!hit && !Array.isArray(step.labels) && wants.length) {
          hit = opts.find(o => norm(o.text).startsWith(wants[0]))
             || opts.find(o => norm(o.text).includes(wants[0]));
        }
        if (!hit) {
          report.push({ref: step.ref, ok: false, error: 'no_option_match',
                       want: requested.map(String).join(' | '),
                       candidates: opts.slice(0, 8).map(o => o.text.trim().slice(0, 40))});
          continue;
        }
        el.value = hit.value;
        fire(el, ['input', 'change', 'blur']);
        report.push({ref: step.ref, ok: el.value === hit.value,
                     want: hit.text.trim(), requested: requested.map(String),
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
  // Tell the main-world dry-run guard that the document now holds entered data, so it
  // starts refusing mutating fetch/XHR. An attribute is the only channel: this runs in the
  // isolated world and its expandos do not cross realms, while the DOM is shared. Folded
  // into this evaluate so it costs nothing — the "one write per form" count is unchanged.
  try { document.documentElement.setAttribute('data-bh-entered', '1'); } catch (e) {}
  return report;
})(__PLAN__)"""

#: Read a combobox's current rendering. `innerText` rather than `value`, because a div
#: widget has no value — what it *shows* is the only thing a user or a check can compare.
_COMBO_STATE_JS = """((ref) => {
  const el = window.__bh && window.__bh.refs[ref];
  if (!el) return null;
  const inner = el.querySelector('input, [contenteditable=true]');
  return {text: (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 120),
          value: inner ? (inner.value ?? inner.textContent) : null,
          expanded: el.getAttribute('aria-expanded'),
          active: el.getAttribute('aria-activedescendant'),
          hasInput: !!inner,
          inputX: inner ? Math.round(inner.getBoundingClientRect().x +
                                     inner.getBoundingClientRect().width / 2) : null,
          inputY: inner ? Math.round(inner.getBoundingClientRect().y +
                                     inner.getBoundingClientRect().height / 2) : null,
          x: Math.round(el.getBoundingClientRect().x + el.getBoundingClientRect().width / 2),
          y: Math.round(el.getBoundingClientRect().y + el.getBoundingClientRect().height / 2)};
})(__REF__)"""

#: The open popup's options, with click coordinates.
#:
#: Scope matters and is not cosmetic. Real widgets portal their listbox to `document.body`,
#: far from the combobox in the DOM, so "search inside the element" finds nothing; but
#: searching the whole document finds every OTHER combobox's options too, and picking one
#: of those silently fills the wrong field. So: the listbox this combobox declares via
#: `aria-controls`/`aria-owns` first, then the single visible listbox, then the document.
_COMBO_OPTIONS_JS = """((ref) => {
  const bh = window.__bh;
  const el = bh && bh.refs[ref];
  if (!el) return {scope: 'none', options: []};
  const id = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
  let root = id ? document.getElementById(id) : null;
  let scope = root ? 'aria-controls' : '';
  if (!root) {
    const boxes = [...document.querySelectorAll('[role=listbox],[role=menu]')].filter(bh.visible);
    if (boxes.length === 1) { root = boxes[0]; scope = 'sole-listbox'; }
  }
  if (!root) { root = document; scope = 'document'; }
  const out = [];
  for (const o of root.querySelectorAll('[role=option],[role=menuitem],li[data-value]')) {
    if (!bh.visible(o)) continue;
    const r = o.getBoundingClientRect();
    out.push({text: (o.innerText || o.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 80),
              x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
              selected: o.getAttribute('aria-selected') === 'true'});
  }
  return {scope, options: out};
})(__REF__)"""


_STEP_CLASS = {"element_gone": Class.ELEMENT_GONE, "no_option_match": Class.NO_OPTION_MATCH,
               "needs_interaction": Class.NEEDS_INTERACTION}

MODES = ("value", "insert", "type")


def _step_class(entry: dict[str, Any]) -> Class:
    """A step that reports no `error` executed cleanly and had its value refused or
    rewritten by the control. That is VALUE_REJECTED, not JS_EXCEPTION — the recovery is
    a different write mode, and calling it a JS exception sent readers looking for a
    stack trace that was never thrown."""
    err = str(entry.get("error") or "")
    if not err:
        return Class.VALUE_REJECTED
    return _STEP_CLASS.get(err, Class.JS_EXCEPTION)


def _typed_write(tab: Tab, ref: str, value: Any, mode: str, timeout: float) -> dict[str, Any]:
    """The `insert`/`type` tiers, as one report entry shaped like the batched writer's.

    Focus is not optional and not the caller's job: `Input.insertText` and
    `dispatchKeyEvent` go to whatever the renderer considers focused, so without this the
    keystrokes land somewhere else entirely and the field is left untouched with no error.
    """
    focused = tab._world_js(
        f"(() => {{const el = window.__bh && __bh.refs[{json.dumps(ref)}]; if (!el) return false;"
        " try { document.documentElement.setAttribute('data-bh-entered', '1'); } catch (e) {}"
        " el.focus(); el.select && el.select(); return true;})()", timeout=timeout)
    if not focused:
        return {"ref": ref, "ok": False, "error": "element_gone", "mode": mode}
    typed = None
    if mode == "insert":
        # Input.insertText is delivered even to a background tab (measured — it is the
        # one raw-input path the renderer does not gate on tab selection).
        tab.cdp("Input.insertText", {"text": str(value)}, timeout=timeout)
    else:
        # Verified per-character events: the renderer drops key events for a tab that is
        # not its window's selected tab, so type_chars checks the `__bh.keys` delivery
        # counter and synthesizes through the DOM when nothing arrived (parallel() puts
        # every worker but at most one in exactly that state).
        typed = tab.type_chars(str(value), ref=ref, timeout=timeout)
    got = tab._world_js(f"(() => {{const el = __bh.refs[{json.dumps(ref)}]; el.blur();"
                        " return String(el.value).slice(0, 80);})()", timeout=timeout)
    want = str(value)
    good = got == want[:80] or got == want or (_digits(got) == _digits(want) != "")
    entry = {"ref": ref, "ok": bool(good), "mode": mode,
             "want": want[:80], "got": got}
    if typed is not None and typed.get("modality") == "dom":
        entry["modality"] = "dom"       # visible in the report, like the click delta's
    if good and got != want[:80]:
        entry["normalized"] = True
    return entry


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


_HUMAN_REVEAL_JS = """await (async () => {
  /* bh-human-reveal */
  const el = window.__bh && __bh.refs[__REF__];
  if (!el) return {ok: false, error: 'element_gone'};
  const before = el.getBoundingClientRect();
  el.scrollIntoView({behavior: 'smooth', block: 'center', inline: 'nearest'});

  // scrollIntoView has no completion promise. Wait until the control's viewport position
  // has stopped changing for a few animation frames, with a hard ceiling for pages whose
  // sticky layout never becomes perfectly still.
  const deadline = performance.now() + 1200;
  let last = before.top, stable = 0;
  await new Promise(resolve => {
    const tick = () => {
      const top = el.getBoundingClientRect().top;
      stable = Math.abs(top - last) < 0.5 ? stable + 1 : 0;
      last = top;
      if (stable >= 4 || performance.now() >= deadline) resolve();
      else requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
  await new Promise(resolve => setTimeout(resolve, __PAUSE_MS__));
  const after = el.getBoundingClientRect();
  return {ok: true, moved: Math.abs(after.top - before.top) >= 0.5,
          top: Math.round(after.top), viewport_height: innerHeight};
})()"""


def _human_reveal(tab: Tab, ref: str, *, timeout: float, pause: float) -> dict[str, Any]:
    """Smoothly centre one field and leave it visible long enough to explain the write."""
    src = (_HUMAN_REVEAL_JS.replace("__REF__", json.dumps(ref))
           .replace("__PAUSE_MS__", str(round(pause * 1000))))
    return tab._world_js(src, timeout=timeout) or {"ok": False, "error": "no_result"}


def _fill_outcome(report: list[dict[str, Any]], fields: int,
                  consequence: dict[str, Any] | None = None) -> Outcome:
    tally = Tally()
    for entry in report:
        if entry.get("ok"):
            tally.record(ok(entry))
        else:
            tally.record(fail(_step_class(entry),
                              entry.get("error") or "value did not stick", **entry))
    observed: dict[str, Any] = {"fields": fields}
    if consequence is not None:
        observed["consequence"] = consequence
    return tally.outcome(value=report, **observed)


def fill_form(tab: Tab, plan: list[dict[str, Any]], *, timeout: float = 30.0,
              recheck: float = 0.15, human_readable: bool = False,
              human_pause: float = 0.18) -> Outcome:
    """One write for the whole plan. Rule 4: OK only when every field verified; PARTIAL
    carries the full per-field report either way (`outcome.value`).

    `recheck` re-reads every field after a settle, in **one** extra evaluate, because the
    immediate `el.value === want` check is measured too early for framework-controlled
    inputs. Measured on jobs.ch: a React phone field rejects `079 123 45 67` outright but
    rewrites `+41791234567` to `+41 79 123 45 67` — so the write succeeded while the
    immediate check said it failed, and a normalising field would otherwise be reported as
    a failure forever. Set `recheck=0` to skip the settle when speed matters more.

    A step may carry `"mode": "insert" | "type"` for a control the one-shot write cannot
    drive — a mask that reformats as you type, a keystroke typeahead. Those cost a round
    trip each and so are done after the batch, but they travel in the same plan and come
    back in the same report: without this the caller had to abandon `fill_form` and hand-
    roll `set_value` per field, which is the batching win thrown away on exactly the forms
    that most need it.

    ``human_readable=True`` deliberately trades the one-pass performance invariant for a
    debuggable recording: each field is smoothly centred, allowed to settle, and then
    written on its own. The default remains the measured fast path and performs no scroll.
    """
    if not plan:
        return ok([], attempted=0, succeeded=0, failed=0)
    bad = [s.get("mode") for s in plan if s.get("mode") and s.get("mode") not in MODES]
    if bad:
        raise ValueError(f"mode must be one of {MODES}, got {bad!r}")
    if human_pause < 0:
        raise ValueError("human_pause must be non-negative")
    if human_readable:
        report: list[dict[str, Any]] = []
        with tab.journal.call("fill_form", n=len(plan), presentation="human_readable"):
            for step in plan:
                reveal = _human_reveal(tab, str(step.get("ref") or ""),
                                       timeout=timeout, pause=human_pause)
                single = fill_form(tab, [step], timeout=timeout, recheck=recheck)
                entry = (single.value[0] if isinstance(single.value, list) and single.value
                         else {"ref": step.get("ref"), "ok": False,
                               "error": "no report entry"})
                report.append({**entry, "presentation": reveal})
        return _fill_outcome(report, len(plan))
    interactive = [(i, s) for i, s in enumerate(plan) if s.get("interaction") == "select"]
    batched = [(i, s) for i, s in enumerate(plan)
               if s.get("mode", "value") == "value" and not s.get("interaction")]
    typed = [(i, s) for i, s in enumerate(plan)
             if s.get("mode", "value") != "value" and not s.get("interaction")]
    batch_plan = [{k: v for k, v in s.items() if k != "mode"} for _, s in batched]

    action_token = tab._action_token()
    refs = [str(step.get("ref") or "") for step in plan]
    src = (_FILL_JS.replace("__PLAN__", json.dumps(batch_plan))
           .replace("__ACTION_TOKEN__", json.dumps(action_token)))
    fuse_consequence = not typed and not interactive
    consequence_source = tab._action_consequence_source(action_token, refs=refs)
    if fuse_consequence and not (recheck > 0 and batch_plan):
        src = ("await (async () => {const report = " + src
               + "; await Promise.resolve(); return {report, consequence: "
               + consequence_source + "};})()")
    inline_consequence: Any = {} if fuse_consequence else None
    with tab.journal.call("fill_form", n=len(plan)):
        written = tab._world_js(src, timeout=timeout) or []
        if isinstance(written, dict) and "report" in written:
            report = written.get("report") or []
            inline_consequence = written.get("consequence") or {}
        else:
            report = written
        if recheck > 0 and batch_plan:
            time.sleep(recheck)
            recheck_source = _RECHECK_JS.replace("__PLAN__", json.dumps(batch_plan))
            if fuse_consequence:
                recheck_source = ("await (async () => {const settled = " + recheck_source
                                  + "; await Promise.resolve(); return {settled, consequence: "
                                  + consequence_source + "};})()")
            rechecked = tab._world_js(recheck_source, timeout=timeout) or []
            if isinstance(rechecked, dict) and "settled" in rechecked:
                settled = rechecked.get("settled") or []
                inline_consequence = rechecked.get("consequence") or {}
            else:
                settled = rechecked
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
        # After the batch: a mask reformats against the value its neighbours already hold.
        typed_reports = {i: _typed_write(tab, s["ref"], s.get("value", s.get("label")),
                                         s["mode"], timeout)
                         for i, s in typed}
        interactive_reports = {}
        for i, step in interactive:
            outcome = select_option(tab, step["ref"], step.get("labels") or
                                    step.get("label") or step.get("value"), timeout=timeout)
            if outcome.ok:
                value = outcome.value
                if isinstance(value, list):
                    value = next((entry for entry in value if isinstance(entry, dict)), {})
                interactive_reports[i] = {"ref": step["ref"], "ok": True,
                                          "interaction": "select", **(value or {})}
            else:
                interactive_reports[i] = {
                    "ref": step["ref"], "ok": False,
                    "error": outcome.cls.value, **outcome.observed,
                }

    merged: list[dict[str, Any]] = []
    slot_of = {i: slot for slot, (i, _) in enumerate(batched)}
    for i, step in enumerate(plan):
        if i in interactive_reports:
            merged.append(interactive_reports[i])
            continue
        if i in typed_reports:
            merged.append(typed_reports[i])
            continue
        slot = slot_of.get(i, -1)
        merged.append(report[slot] if 0 <= slot < len(report)
                      else {"ref": step.get("ref"), "ok": False, "error": "no report entry"})

    consequence = (tab._shape_action_consequence(inline_consequence)
                   if inline_consequence is not None else tab._action_consequence(
                       action_token, refs=refs, timeout=timeout))
    verified = sum(bool(entry.get("ok")) for entry in merged)
    consequence["write_validation"] = {
        "attempted": len(plan), "verified": verified, "failed": len(plan) - verified,
    }
    if verified == len(plan):
        consequence.update(effect="validation", verified=True)
    elif verified:
        consequence.update(effect="partial_validation", verified=False)
    else:
        consequence.update(effect="validation_failed", verified=False)
    return _fill_outcome(merged, len(plan), consequence)     # value = the FULL report


def select_option(tab: Tab, ref: str, label: str | list[str], *, timeout: float = 10.0,
                  type_to_filter: bool | None = None,
                  settle: float = 0.25) -> Outcome:
    """Operate an ARIA combobox: open it, find the option, click it, verify.

    The gap this closes. `form_schema` flags a `role=combobox` widget
    `needs_interaction` and `fill_form` refuses it — correctly, since a div has no value
    to set and writing one throws `Illegal invocation`. But nothing could then *act* on
    it, so the harness diagnosed a dead end and offered no way through. Whole forms on
    SmartRecruiters, Workday and Ashby are built this way.

    A native `<select>` is delegated to `fill_form`, so one call handles both kinds and a
    caller does not have to branch on `kind` first.

    `type_to_filter` types the label into the widget's inner input before reading options —
    which is how a typeahead with thousands of entries renders any at all. Auto-detected
    when the popup opens empty and the widget has an input; pass it explicitly to force.
    """
    # The marker makes this probe identifiable. Without it no substring is unique —
    # `_FILL_JS` also contains `el.tagName.toLowerCase()` in its combobox branch, so a
    # test double dispatching on the obvious token answered the batch write with a tag
    # name and `report` came back as a string.
    action_token = tab._action_token()
    probe = tab._world_js(
        f"/* bh-probe:kind */ (() => {{const bh=window.__bh;"
        f" if(bh) {{bh.actionStarts=bh.actionStarts||{{}};"
        f" if(bh.beginAction) bh.beginAction({json.dumps(action_token)});"
        f" else bh.actionStarts[{json.dumps(action_token)}]=bh.mutations||0;}}"
        f" const e = bh && bh.refs[{ref!r}];"
        " return e ? e.tagName.toLowerCase() : null;})()", timeout=timeout)
    if probe is None:
        consequence = tab._action_consequence(action_token, refs=[ref], timeout=timeout)
        consequence.update(effect="selection_failed", verified=False)
        return fail(Class.ELEMENT_GONE, f"no element registered for ref {ref!r}", ref=ref,
                    consequence=consequence)
    requested = [str(item) for item in label] if isinstance(label, list) else [str(label)]
    if probe == "select":
        # The native-select branch starts its own fused write marker. Consume this probe
        # marker first so the page-side bounded map cannot accumulate abandoned actions.
        tab._action_consequence(action_token, refs=[ref], timeout=timeout)
        key = "labels" if isinstance(label, list) else "label"
        return fill_form(tab, [{"ref": ref, key: label}], timeout=timeout)

    # The chosen label is applicant data. Keep only mechanical cardinality in the
    # journal; the verified outcome remains available to the caller, not the trace.
    with tab.journal.call("select_option", ref=ref, choices=len(requested)):
        before = tab._world_js(_COMBO_STATE_JS.replace("__REF__", json.dumps(ref)),
                               timeout=timeout) or {}
        # A coordinate click, not `el.click()`: these widgets listen for pointer events and
        # many ignore a synthetic click entirely (the same reason D4 makes clicks
        # compositor-level so they pass through shadow roots).
        tab.click_at(before.get("x", 0), before.get("y", 0), settle=settle, timeout=timeout)

        found = tab._world_js(_COMBO_OPTIONS_JS.replace("__REF__", json.dumps(ref)),
                              timeout=timeout) or {"options": [], "scope": "none"}
        wants_typing = type_to_filter
        if wants_typing is None:
            wants_typing = not found["options"] and bool(before.get("hasInput"))
        if wants_typing and before.get("hasInput"):
            # Typeahead: the list is empty until it is filtered. Real key events, because
            # that is the only write mode a keystroke-driven typeahead can see (D3) —
            # and verified ones, because the renderer drops key events for a background
            # tab and this path then reported "the popup exposed no options", an error
            # that sent the reader to the page instead of to the tab state.
            tab.type_chars(requested[0], timeout=timeout, settle=settle)
            found = tab._world_js(_COMBO_OPTIONS_JS.replace("__REF__", json.dumps(ref)),
                                  timeout=timeout) or {"options": [], "scope": "none"}

        options = found.get("options") or []
        if not options:
            _dismiss(tab, timeout)
            consequence = tab._action_consequence(action_token, refs=[ref], timeout=timeout)
            consequence.update(effect="selection_failed", verified=False)
            return fail(Class.NEEDS_INTERACTION,
                        "the popup exposed no options to choose from",
                        ref=ref, want=requested, scope=found.get("scope"),
                        typed=bool(wants_typing), consequence=consequence)

        def norm(t: str) -> str:
            return " ".join(str(t).split()).lower()
        wants = [norm(item) for item in requested]
        hit = next((option for want in wants for option in options
                    if norm(option["text"]) == want), None)
        if hit is None and not isinstance(label, list):
            hit = (next((o for o in options if norm(o["text"]).startswith(wants[0])), None)
                   or next((o for o in options if wants[0] in norm(o["text"])), None))
        if hit is None:
            # Same contract as a native select: never fall back to "the first one".
            _dismiss(tab, timeout)
            consequence = tab._action_consequence(action_token, refs=[ref], timeout=timeout)
            consequence.update(effect="selection_failed", verified=False)
            return fail(Class.NO_OPTION_MATCH,
                        f"no option matching {requested!r} among {len(options)}",
                        ref=ref, want=requested,
                        candidates=[o["text"] for o in options[:8]],
                        options_count=len(options), scope=found.get("scope"),
                        consequence=consequence)

        tab.click_at(hit["x"], hit["y"], settle=settle, timeout=timeout)
        after = tab._world_js(_COMBO_STATE_JS.replace("__REF__", json.dumps(ref)),
                              timeout=timeout) or {}

    # Verify the WIDGET changed, not that we clicked something: a click that missed and a
    # click that landed look identical without this.
    shown = str(after.get("value") or after.get("text") or "")
    changed = shown != str(before.get("value") or before.get("text") or "")
    matched = norm(shown) == norm(hit["text"]) or norm(hit["text"]) in norm(shown)
    if changed or matched:
        consequence = tab._action_consequence(action_token, refs=[ref], timeout=timeout)
        consequence.update(effect="validation", verified=True)
        return ok({"ref": ref, "want": requested, "got": hit["text"], "shown": shown[:80]},
                  options_count=len(options), consequence=consequence)
    consequence = tab._action_consequence(action_token, refs=[ref], timeout=timeout)
    consequence.update(effect="selection_failed", verified=False)
    return fail(Class.NEEDS_INTERACTION,
                "the option was clicked but the widget still shows its old value",
                ref=ref, want=requested, clicked=hit["text"], shown=shown[:80],
                consequence=consequence)


def _dismiss(tab: Tab, timeout: float) -> None:
    """Close an open popup. A listbox left open covers the page and swallows the next
    click, so failing to select must not also break whatever is attempted afterwards."""
    try:
        tab.press_key("Escape", timeout=timeout)
    except Exception:  # noqa: BLE001, S110 — best effort; the real failure is the caller's
        pass


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
    if mode not in MODES:
        raise ValueError(f"mode must be value|insert|type, got {mode!r}")
    if mode == "value":
        return fill_form(tab, [{"ref": ref, "value": value}], timeout=timeout,
                         recheck=recheck)
    action_token = tab._action_token()
    tab._start_action(action_token, timeout=timeout)
    entry = _typed_write(tab, ref, value, mode, timeout)
    consequence = tab._action_consequence(action_token, refs=[ref], timeout=timeout)
    if entry.get("error") == "element_gone":
        consequence.update(effect="validation_failed", verified=False)
        return fail(Class.ELEMENT_GONE, f"no element registered for ref {ref!r}", ref=ref,
                    consequence=consequence)
    if entry["ok"]:
        consequence.update(effect="validation", verified=True)
        return ok({"ref": ref, "got": entry["got"]}, mode=mode,
                  consequence=consequence)
    consequence.update(effect="validation_failed", verified=False)
    return fail(Class.VALUE_REJECTED, "value did not stick", ref=ref, mode=mode,
                want=entry["want"], got=entry["got"], consequence=consequence)


_SECRET_FILL_JS = """((ref, secret) => {
  const el = window.__bh && window.__bh.refs[ref];
  if (!el) return {ok: false, error: 'element_gone'};
  if (!(el instanceof HTMLInputElement) || el.type !== 'password')
    return {ok: false, error: 'not_password'};
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
  el.focus();
  if (setter && setter.set) setter.set.call(el, secret); else el.value = secret;
  for (const type of ['input', 'change', 'blur'])
    el.dispatchEvent(type === 'input' ? new InputEvent(type, {bubbles: true})
                                      : new Event(type, {bubbles: true}));
  try { document.documentElement.setAttribute('data-bh-entered', '1'); } catch (e) {}
  return {ok: el.value === secret, nonempty: !!el.value, kind: el.type};
})(__REF__, __SECRET__)"""


def set_secret_from_keychain(tab: Tab, ref: str, *, service: str, account: str,
                             timeout: float = 20.0) -> Outcome:
    """Fill a password without putting it in source, journals, outcomes or reports.

    The macOS Keychain lookup and the CDP write happen inside this helper.  Only the
    credential locator crosses the public API; CDP telemetry already records parameter
    shape and byte counts rather than values.
    """
    with tab.journal.call("set_secret", ref=ref, provider="keychain",
                          service=service, account=account):
        found = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", service, "-a", account],
            check=False, capture_output=True)
        if found.returncode != 0 or not found.stdout:
            return fail(Class.NEEDS_INTERACTION, "keychain credential is unavailable",
                        ref=ref, provider="keychain", service=service, account=account)
        secret = found.stdout.rstrip(b"\r\n").decode("utf-8")
        try:
            result = tab._world_js(
                _SECRET_FILL_JS.replace("__REF__", json.dumps(ref)).replace(
                    "__SECRET__", json.dumps(secret)), timeout=timeout) or {}
        finally:
            secret = ""
        if result.get("error") == "element_gone":
            return fail(Class.ELEMENT_GONE, f"no element registered for ref {ref!r}", ref=ref)
        if result.get("error") == "not_password":
            return fail(Class.VALUE_REJECTED, "secret target is not a password control", ref=ref)
        if result.get("ok") and result.get("nonempty"):
            return ok({"ref": ref, "filled": True, "secret_source": "keychain"})
        return fail(Class.VALUE_REJECTED, "secret value did not stick", ref=ref)
