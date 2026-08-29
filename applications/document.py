"""What KIND of form this is — the judgment core stopped making.

`form_schema()` reports facts: which controls exist, what they are labelled, whether any
of them can be submitted. Whether those controls are a job application, a login wall, or a
newsletter box is knowledge about recruiting software, and it used to be compiled into the
harness's page read.

It is composed onto the same script rather than run after it. `forms._SCHEMA_BODY_JS`
extracts the DOM and stops without returning, so this appends its own classification and
closing: the page is read once, judged twice, and still costs one round trip. Splitting it
into two evaluations would have paid for the boundary in latency, which is the trade this
whole exercise exists to avoid.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from harness.core.outcome import NotAForm
from harness.ops import forms
from harness.ops.page import Tab

_APPLICATION_VERDICT_JS = """
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

#: One evaluation: the harness's factual extraction plus this module's classification.
SCHEMA_JS = forms._SCHEMA_BODY_JS + _APPLICATION_VERDICT_JS


_PREPARE_JS = """(() => {
  const schema = __SCHEMA__;
  const bh = window.__bh;
  const fileInputs = [...document.querySelectorAll('input[type=file]')].map(el => {
    const ref = bh.ref(el);
    const label = el.id ? document.querySelector('label[for="' + CSS.escape(el.id) + '"]') : null;
    const labelText = (label && label.innerText || el.getAttribute('aria-label') || '').trim();
    return {el, ref, name: el.name || el.id || 'file',
            label: labelText, accept: el.accept || '', multiple: !!el.multiple,
            required: !!el.required || labelText.includes('*')};
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
  // Action cache (2026-08-29): a skill may name the control that led to a form on this
  // host before — by label and/or by a structural selector. A hinted control outranks
  // the heuristic pick; a hinted label outside APPLY_TEXT ("I'm interested") still
  // qualifies. The heuristic stays the fallback, so a stale hint costs nothing.
  const hint = (__APPLY_HINT__) || {};
  const hintLabels = (hint.labels || []).map(t => String(t).trim().toLowerCase()).filter(Boolean);
  const hintSelectors = (hint.selectors || []).filter(Boolean);
  const hintBoost = el => {
    let b = 0;
    const l = labelOf(el).trim().toLowerCase();
    if (l && hintLabels.some(h => h === l || (h.length >= 6 && l.includes(h)))) b += 5;
    for (const sel of hintSelectors) { try { if (el.matches(sel)) { b += 6; break; } } catch (e) {} }
    return b;
  };
  const selectorOf = el => {
    if (!el) return null;
    if (el.id && !/\\d{4,}/.test(el.id)) return '#' + CSS.escape(el.id);
    const tag = el.tagName.toLowerCase();
    for (const attr of ['data-testid', 'data-test', 'data-automation-id', 'name', 'aria-label']) {
      const v = el.getAttribute(attr); if (v && v.length < 80) return `${tag}[${attr}="${v.replace(/"/g, '\\"')}"]`;
    }
    const parts = []; let node = el;
    for (let depth = 0; node && node.nodeType === 1 && depth < 4; depth++) {
      const t = node.tagName.toLowerCase();
      if (node.id && !/\\d{4,}/.test(node.id)) { parts.unshift('#' + CSS.escape(node.id)); break; }
      let i = 1; for (let s = node.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === node.tagName) i++;
      parts.unshift(`${t}:nth-of-type(${i})`); node = node.parentElement;
    }
    return parts.join(' > ');
  };
  // selectorOf is a const above this line's scan: fill the selector in here, drop the element.
  for (const f of fileInputs) { f.selector = selectorOf(f.el); delete f.el; }
  let apply = null, bestScore = 0;
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.getAttribute('href') || '';
    if (!href || href.startsWith('#') || /^(javascript|mailto|tel):/i.test(href)) continue;
    let s = scoreLabel(labelOf(a));
    const hb = hintBoost(a);
    if (hb) s = Math.max(s, 3) + hb;
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
    const hb = hintBoost(b);
    if (hb) s = Math.max(s, 3) + hb;
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
                    tag: ctl.tagName.toLowerCase(), painted: ctlPainted,
                    selector: selectorOf(ctl), hinted: hintBoost(ctl) > 0};
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
          apply_link_label: apply ? labelOf(apply).slice(0, 60) : null,
          apply_link_selector: apply ? selectorOf(apply) : null,
          apply_link_hinted: apply ? hintBoost(apply) > 0 : false,
          apply_control: applyControl, application_urls: applicationUrls.slice(0, 12)};
})()""".replace("__SCHEMA__", SCHEMA_JS)


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


def prepare_document(tab: Tab, *, guard_submit: bool = True,
                     timeout: float = 20.0,
                     apply_hint: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return all application perception data in one round trip.

    ``guard_submit`` remains source-compatible, but cannot disable the session's automatic
    dry-run boundary. Every document is guarded before page script in ``Tab``.
    ``apply_hint`` (``{"labels": [...], "selectors": [...]}``) is the action cache: a
    control matching it outranks the heuristic pick; absent or stale, nothing changes.
    """
    source = _PREPARE_JS.replace("__APPLY_HINT__", json.dumps(apply_hint or None))
    return tab._world_js(source, timeout=timeout)


def application_schema(tab: Tab, *, timeout: float = 10.0) -> dict[str, Any]:
    """`{verdict, fields, files}` where the verdict also says what kind of form it is."""
    return tab._world_js(SCHEMA_JS, timeout=timeout) or {}


def require_application_form(schema: dict[str, Any]) -> dict[str, Any]:
    """Raise `NotAForm` unless this is an application — the 404-page guard, domain edition."""
    verdict = schema.get("verdict") or {}
    if not verdict.get("is_application"):
        raise NotAForm(verdict.get("reason", "no application form identified"), **verdict)
    return schema
