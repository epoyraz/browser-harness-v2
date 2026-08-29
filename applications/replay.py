"""Replay: the second application at the same employer skips perception.

`run_application` discovers — navigate, probe, scan the DOM twice, plan, fill. The same
tenant serves the same form for every posting, so one discovery is enough to *record* a
program: the apply control at each hop and every field the planner filled, each by a
selector **and** by its label. `replay_application` executes that program blind —
navigate, click, one batched fill — and verifies. When verification fails the caller falls
back to discovery and re-records; a recording is a bet, never a claim.

A recording holds no applicant data. It stores what each field *is* (selector, label,
kind, name, semantic, options) and the planner (`ontology.plan_for`) re-derives the
values at replay time from whoever is applying now — so a program recorded as Max
Mustermann fills Martina Musterfrau's details when she applies, and never his.

What can go wrong, and what catches it
--------------------------------------
* the apply control's selector is gone (redesign)   -> resolved by its label (`find`);
                                                       failing that: fallback
* landing is a wall / expired posting               -> state probe says wall: fallback
* the form changed shape                            -> a field resolves to another kind of
                                                       control, or a new *required* field
                                                       appears after the fill: fallback
* ids drift but labels hold (React-generated ids)   -> fields resolved by label from one
                                                       `form_schema()` read (self-heal)
* a value is refused (mask, format)                 -> the batched fill reports it: fallback
* another form for another job family               -> recordings keyed by host AND form
                                                       fingerprint; several per host, tried
                                                       in success order
* a flaky site                                      -> two consecutive failures retire a
                                                       recording; the next discovery re-records

Telemetry: every journal row written during a replay carries ``mode: replay`` and the
recording's fingerprint; one ``application_replayed`` note per call carries the verdict.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from applications import document, ontology
from applications import state as application_state
from harness.core.outcome import HarnessError
from harness.ops import forms

RETIRE_AFTER_FAILURES = 2
#: Field attributes a recording keeps. Deliberately no `value`: values belong to the
#: applicant, the recording belongs to the site.
_FIELD_KEYS = ("selector", "label", "kind", "name", "required", "semantic", "options_sample",
               "options_count", "widget", "needs_interaction", "autocomplete", "group_label",
               "label_source", "placeholder_first")


def host_of(url: str) -> str:
    return (urlsplit(url or "").hostname or "").lower()


def _norm_label(text: Any) -> str:
    return re.sub(r"[\s*✱:]+", " ", str(text or "")).strip().lower()


def _fingerprint(fields: list[dict[str, Any]], steps: list[dict[str, Any]]) -> str:
    """Structure only — selectors, kinds and required flags, never values."""
    sig = "|".join(sorted(f"{f.get('selector')}:{f.get('kind')}:{int(bool(f.get('required')))}" for f in fields))
    sig += "||" + "|".join(str(s.get("selector") or s.get("url")) for s in steps)
    return hashlib.sha256(sig.encode("utf-8")).hexdigest()[:16]


def _label_key(label: Any) -> str:
    return re.sub(r"[\s*:]+", " ", str(label or "")).strip().lower()


def real_gaps(unplanned: list[Any], planned: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Required controls that no planned field answers.

    Forms render one question as several controls: a native ``<select>`` plus the widget
    button that mirrors it (jQuery UI ``#country-button`` next to ``#country``), a Ja/Nein
    pair, a select and its search box. The planner fills one of them and the others are
    not gaps. A control counts as a gap only when no planned field carries its label, and
    one label is reported once.
    """
    covered = {_label_key(f.get("label")) for f in planned if isinstance(f, dict) and f.get("label")}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for f in unplanned:
        if not isinstance(f, dict):
            continue
        key = _label_key(f.get("label"))
        if key and (key in covered or key in seen):
            continue
        if key:
            seen.add(key)
        out.append(f)
    return out


def record_from_result(value: dict[str, Any]) -> dict[str, Any] | None:
    """A recording from one `form_processed` collector record, or None if it cannot be
    replayed (no selectors — records older than 2026-08-29 23:00 carry refs only)."""
    if value.get("status") != "form_processed":
        return None
    hops = [h for h in (value.get("hops") or []) if isinstance(h, dict)]
    steps: list[dict[str, Any]] = []
    for i, h in enumerate(hops[:-1]):
        nxt = hops[i + 1]
        if not nxt.get("is_application"):
            continue
        kind = (h.get("transition") or {}).get("kind")
        if kind in ("control", "new_target"):
            ctl = h.get("apply_control") or {}
            if not (ctl.get("selector") or ctl.get("label")):
                return None
            steps.append({"action": "click", "selector": ctl.get("selector"), "label": ctl.get("label"),
                          "new_target": kind == "new_target"})
        elif kind in ("link", "fallback_link"):
            # The link's URL is posting-specific (job 5996's form is not job 6091's); the
            # link's selector/label on the posting page is what repeats across postings.
            if not (h.get("apply_link_selector") or h.get("apply_link_label")):
                return None
            steps.append({"action": "click", "selector": h.get("apply_link_selector"),
                          "label": h.get("apply_link_label"), "new_target": False})
        elif kind == "candidate_link":
            return None  # a route rule, which discovery already handles cheaply
    schema = value.get("schema") or {}
    by_ref = {f.get("ref"): f for f in (schema.get("fields") or []) if isinstance(f, dict)}
    semantic_by_ref = {a.get("ref"): a.get("semantic") for a in (value.get("field_audit") or [])
                       if isinstance(a, dict)}
    planned_refs = [i.get("ref") for i in (value.get("plan") or []) if isinstance(i, dict)]
    fields = []
    for ref in planned_refs:
        f = by_ref.get(ref)
        if not f or not (f.get("selector") or f.get("label")):
            return None
        rec = {k: f.get(k) for k in _FIELD_KEYS if f.get(k) is not None}
        rec["semantic"] = semantic_by_ref.get(ref) or rec.get("semantic")
        fields.append(rec)
    if not fields:
        return None
    # Required fields discovery saw but could not plan (no profile answer, a judgement
    # call, a password): kept with their labels so a preflight can name them.
    required_unplanned = real_gaps(
        [{"selector": f.get("selector"), "label": f.get("label"), "kind": f.get("kind"),
          "semantic": semantic_by_ref.get(f.get("ref"))}
         for f in by_ref.values()
         if f.get("required") and f.get("ref") not in planned_refs
         and f.get("kind") not in ("checkbox", "radio", "file")],
        fields)
    # File inputs with their labels, so a replay can hand each one the right document
    # (CV, letter, certificates) through a resolver. Never a path: documents are the
    # applicant's, not the recording's.
    files = [{"selector": f.get("selector"), "label": f.get("label"), "name": f.get("name"),
              "required": bool(f.get("required")), "multiple": bool(f.get("multiple"))}
             for f in (value.get("file_inputs") or []) if isinstance(f, dict) and f.get("selector")]
    return {"host": host_of(value.get("start_url") or ""), "company": value.get("company"),
            "recorded_from": value.get("job_id"), "form_url": value.get("landed_url"),
            "language": value.get("language") or "en",
            "steps": steps, "fields": fields, "files": files,
            "required_unplanned": required_unplanned,
            "fingerprint": _fingerprint(fields, steps),
            "hops": len(hops), "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "stats": {"uses": 0, "successes": 0, "consecutive_failures": 0, "last_ok": None}}


@contextmanager
def _as_applicant(applicant: Any | None, profile: dict[str, Any] | None):
    """Plan as a given applicant without leaving them configured afterwards.

    The planner answers from `ontology.APPLICANT`/`PROFILE`; a recording is per site and a
    verdict is per person, so the person is a parameter here, not ambient state. ``None``
    keeps whoever is configured (the collector's applicant)."""
    if applicant is None and profile is None:
        yield
        return
    saved = (ontology.APPLICANT, ontology.PROFILE, ontology.CV)
    ontology.configure(applicant if applicant is not None else saved[0],
                       profile if profile is not None else saved[1], saved[2])
    try:
        yield
    finally:
        ontology.configure(*saved)


def plan_for_recording(recording: dict[str, Any], refs: dict[int, str],
                       language: str | None = None, *, applicant: Any | None = None,
                       profile: dict[str, Any] | None = None
                       ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The fill plan for one applicant: the recording's fields, given live refs, through
    the same planner discovery uses. Pure apart from the applicant it is asked about."""
    live = []
    for index, f in enumerate(recording.get("fields") or []):
        ref = refs.get(index)
        if not ref:
            continue
        live.append({**{k: v for k, v in f.items() if k != "semantic"}, "ref": ref})
    schema = {"fields": live, "files": [], "verdict": {"is_application": True, "fields": len(live)}}
    with _as_applicant(applicant, profile):
        plan, audit = ontology.plan_for(schema, language or recording.get("language") or "en")
    return list(plan or []), list(audit or [])


def missing_required(recording: dict[str, Any], language: str | None = None, *,
                     applicant: Any | None = None,
                     profile: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Required fields of a recorded form that *this* applicant cannot answer.

    A property of the pair (recording, applicant): the recording knows every required
    field and its semantic, the applicant's profile knows the answers, and the planner
    decides what it would write. Pure and browser-free — run it before replaying, so an
    application that will stop at an unanswered required field costs no navigation.
    """
    fields = list(recording.get("fields") or [])
    refs = {i: f"pre{i}" for i in range(len(fields))}
    _plan, audit = plan_for_recording(recording, refs, language, applicant=applicant, profile=profile)
    missing: list[dict[str, Any]] = []
    unanswered = {"missing_profile", "needs_judgement", "unclassified"}
    for row in audit:
        if row.get("required") and row.get("status") in unanswered:
            missing.append({"label": row.get("label"), "semantic": row.get("semantic"),
                            "status": row.get("status")})
    # Fields discovery could not plan for *the recording* applicant are re-asked for this
    # one: a salutation Max's profile lacked is not missing for Martina, whose profile has it.
    with _as_applicant(applicant, profile):
        for f in real_gaps(recording.get("required_unplanned") or [], recording.get("fields") or []):
            sem = f.get("semantic")
            if not sem or sem == "unclassified":
                # The recording's verdict is as old as the recording; the ontology may have
                # learnt the label since (a bare French "Lieu" became `city` on 2026-08-29).
                try:
                    learnt = ontology.semantic({"label": f.get("label"), "name": f.get("name"),
                                                "kind": f.get("kind")})
                except Exception:  # noqa: BLE001 — a classifier error is not a missing field
                    learnt = None
                sem = learnt if learnt and learnt != "unclassified" else None
            answer = ontology.profile_value(sem, language or recording.get("language") or "en") if sem else None
            if answer is not None and not answer.known_absent and answer.value not in (None, ""):
                continue
            missing.append({"label": f.get("label"), "semantic": sem,
                            "status": "unplanned_at_recording" if sem else "unclassified"})
    return missing


class RecordingStore:
    """Per-host lists of recordings with success counters; JSON on disk, thread-safe."""

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._by_host: dict[str, list[dict[str, Any]]] = {}
        for p in self.dir.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            recs = data if isinstance(data, list) else [data]
            for rec in recs:
                if isinstance(rec, dict) and rec.get("host"):
                    self._by_host.setdefault(rec["host"], []).append(rec)

    def _path(self, host: str) -> Path:
        return self.dir / (host.replace(":", "_") + ".json")

    def _flush(self, host: str) -> None:
        self._path(host).write_text(json.dumps(self._by_host.get(host, []), ensure_ascii=False, indent=1),
                                    encoding="utf-8")

    def candidates(self, host: str) -> list[dict[str, Any]]:
        """Live recordings for a host, best first: most successes, fewest recent failures."""
        with self._lock:
            recs = [r for r in self._by_host.get(host, [])
                    if (r.get("stats") or {}).get("consecutive_failures", 0) < RETIRE_AFTER_FAILURES]
            return sorted(recs, key=lambda r: (-(r.get("stats") or {}).get("successes", 0),
                                               (r.get("stats") or {}).get("consecutive_failures", 0)))

    def add(self, rec: dict[str, Any]) -> None:
        with self._lock:
            recs = self._by_host.setdefault(rec["host"], [])
            for i, old in enumerate(recs):
                if old.get("fingerprint") == rec.get("fingerprint"):
                    rec["stats"] = old.get("stats") or rec["stats"]
                    rec["stats"]["consecutive_failures"] = 0
                    recs[i] = rec
                    break
            else:
                recs.append(rec)
            self._flush(rec["host"])

    def note(self, rec: dict[str, Any], ok: bool) -> None:
        with self._lock:
            stats = rec.setdefault("stats", {"uses": 0, "successes": 0, "consecutive_failures": 0, "last_ok": None})
            stats["uses"] += 1
            if ok:
                stats["successes"] += 1
                stats["consecutive_failures"] = 0
                stats["last_ok"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            else:
                stats["consecutive_failures"] += 1
            self._flush(rec["host"])

    def count(self) -> int:
        return sum(len(v) for v in self._by_host.values())


def _attach(tab: Any, files: list[Any], resolver: Callable[[dict[str, Any]], Any], *,
            timeout: float) -> dict[str, Any]:
    """Attach the resolver's documents to the recorded file inputs; evidence per input,
    never a submit. Older recordings stored bare selectors; those get an empty label."""
    rows: list[dict[str, Any]] = []
    if not files:
        # Recordings made before file inputs were recorded (or a form that grew one): scan
        # the live page instead of skipping the documents.
        try:
            got = tab.extract("input[type=file]", {"name": ".@name", "id": ".@id", "multiple": ".@multiple",
                                                   "required": ".@required", "label": ".@aria-label"}, limit=8)
            files = [{"ref": r.get("ref"), "name": r.get("name") or r.get("id"), "label": r.get("label") or "",
                      "multiple": r.get("multiple") is not None, "required": r.get("required") is not None,
                      "selector": None, "live": True}
                     for r in ((got.get("rows") if isinstance(got, dict) else None) or []) if r.get("ref")]
        except HarnessError:
            files = []
    for item in files:
        item = {"selector": item} if isinstance(item, str) else dict(item)
        row: dict[str, Any] = {"selector": item.get("selector"), "label": item.get("label")}
        try:
            paths = resolver(item)
            if not paths:
                row["status"] = "unmapped"
            else:
                found = {"ref": item["ref"]} if item.get("live") else _resolve(tab, item.get("selector"))
                if not found or not found.get("ref"):
                    row["status"] = "input_not_found"
                else:
                    outcome = tab.upload_file(found["ref"], paths, timeout=min(timeout, 25.0))
                    ok = bool(getattr(outcome, "ok", True))
                    row["status"] = "attached" if ok else "rejected"
                    row["files"] = [Path(x).name for x in ([paths] if isinstance(paths, str) else paths)]
        except Exception as error:  # noqa: BLE001 — an upload failure is evidence, not a crash
            row["status"] = "error"
            row["error"] = f"{type(error).__name__}: {str(error)[:80]}"
        rows.append(row)
    return {"attempted": len(rows), "succeeded": sum(1 for r in rows if r.get("status") == "attached"),
            "rows": rows}


def _settle(tab: Any, fields: list[dict[str, Any]], timeout: float) -> dict[str, Any]:
    """Wait for the recorded form itself: its first field present (event-driven, fires at
    DOMContentLoaded on most ATS pages) — and only when that never comes, the application
    state machine, which is what tells a wall from a slow page. Waiting for `load` cost
    15-21 s per replay on Umantis pages whose fill then took 170 ms (2026-08-29)."""
    first = next((f.get("selector") for f in fields if isinstance(f, dict) and f.get("selector")), None)
    if first:
        try:
            tab.wait_for(first, state="present", timeout=min(float(timeout), 10.0))
            return {"state": "form", "via": "field_present"}
        except HarnessError:
            pass
    return application_state.wait_for_application_state(tab, timeout=min(float(timeout), 12.0))


def _resolve(tab: Any, selector: str | None) -> dict[str, Any] | None:
    if not selector:
        return None
    try:
        got = tab.extract(selector, {"t": ".", "tag": ".@tagName", "type": ".@type", "name": ".@name",
                                     "href": ".@href"}, limit=1)
    except HarnessError:
        return None
    rows = got.get("rows") if isinstance(got, dict) else None
    return rows[0] if rows else None


def _kind_matches(recorded_kind: str | None, row: dict[str, Any]) -> bool:
    tag = str(row.get("tag") or "").lower()
    typ = str(row.get("type") or "").lower()
    if not recorded_kind or not tag:
        return True
    if recorded_kind == "select":
        return tag == "select"
    if recorded_kind == "textarea":
        return tag == "textarea"
    if recorded_kind in ("radio", "checkbox", "file", "email", "tel", "date", "number"):
        return tag == "input" and (typ == recorded_kind or not typ)
    if recorded_kind == "text":
        return tag == "input"
    return True


def match_fields_by_label(recorded: list[dict[str, Any]], live_fields: list[dict[str, Any]]
                          ) -> dict[int, str]:
    """Self-heal: recorded field index -> live ref, by normalised label and kind. Pure."""
    out: dict[int, str] = {}
    taken: set[str] = set()
    by_label: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for f in live_fields:
        key = (_norm_label(f.get("label")), str(f.get("kind") or ""))
        by_label.setdefault(key, []).append(f)
    for index, f in enumerate(recorded):
        key = (_norm_label(f.get("label")), str(f.get("kind") or ""))
        for live in by_label.get(key, []):
            ref = live.get("ref")
            if ref and ref not in taken:
                out[index] = ref
                taken.add(ref)
                break
        else:
            # same label, any kind (a text box that became an email box is still the field)
            for (label, _kind), lives in by_label.items():
                if label == key[0] and label:
                    for live in lives:
                        ref = live.get("ref")
                        if ref and ref not in taken:
                            out[index] = ref
                            taken.add(ref)
                            break
                    if index in out:
                        break
    return out


def _find_by_label(tab: Any, label: str | None) -> str | None:
    if not label:
        return None
    try:
        hits = tab.find(text=str(label)[:60], max_len=80, limit=5)
    except HarnessError:
        return None
    rows = hits if isinstance(hits, list) else (hits.get("value") or hits.get("hits") or []) if isinstance(hits, dict) else []
    for row in rows:
        if isinstance(row, dict) and row.get("ref"):
            return row["ref"]
    return None


def replay_application(session: Any, url: str, recording: dict[str, Any], *,
                       timeout: float = 20.0, fill_timeout: float = 30.0,
                       verify: bool = True, language: str | None = None,
                       preflight: bool = True, applicant: Any | None = None,
                       profile: dict[str, Any] | None = None,
                       human_readable: bool = False, human_pause: float = 0.18,
                       file_resolver: Callable[[dict[str, Any]], str | list[str] | None] | None = None,
                       ) -> dict[str, Any]:
    """Execute a recording for a new posting URL. Never submits (the dry-run guard holds).

    ``file_resolver(item) -> path | [paths] | None`` attaches documents to the file inputs
    the recording saw (``item`` carries selector/label/name/required/multiple). Without a
    resolver no file input is touched — choosing a file can transfer it before any submit.

    ``human_readable=True`` reveals and types one field at a time (for a watchable
    recording) instead of the one batched write a replay normally does.

    Returns ``{ok, stage, ms, filled, planned, missing, changed, healed, reason}``; ``ok``
    means every recorded field resolved (by selector, or by label), the batched fill
    reported success, and (with ``verify``) no new required field is empty. Anything less
    is the caller's cue to fall back to `run_application`. One ``application_replayed``
    journal note per call carries the verdict.
    """
    # Early return, before any navigation: the recording lists every required field and
    # the profile is known, so an application that would stop at an unanswered required
    # field is reported here — with the field names — at zero browser cost.
    if preflight:
        gaps = missing_required(recording, language, applicant=applicant, profile=profile)
        if gaps:
            out = {"ok": False, "stage": "preflight", "planned": len(recording.get("fields") or []),
                   "filled": 0, "missing": [], "changed": [], "healed": 0, "ms": 0.0,
                   "recording": recording.get("recorded_from"), "missing_required": gaps,
                   "reason": "applicant cannot answer required field(s): "
                             + ", ".join(str(g.get("label") or g.get("semantic")) for g in gaps[:5])}
            session.journal.write("note", event="application_replay_preflight", url=url,
                                  missing=[g.get("label") for g in gaps],
                                  recording=recording.get("fingerprint"))
            return out
    out = _replay(session, url, recording, timeout=timeout, fill_timeout=fill_timeout,
                  verify=verify, language=language, applicant=applicant, profile=profile,
                  human_readable=human_readable, human_pause=human_pause,
                  file_resolver=file_resolver)
    session.journal.write("note", event="application_replayed", url=url, ok=out["ok"],
                          stage=out["stage"], reason=out.get("reason"), filled=out["filled"],
                          planned=out["planned"], healed=out.get("healed"), ms=out["ms"],
                          recording=recording.get("fingerprint"),
                          recorded_from=recording.get("recorded_from"))
    return out


def _replay(session: Any, url: str, recording: dict[str, Any], *, timeout: float,
            fill_timeout: float, verify: bool, language: str | None,
            applicant: Any | None = None, profile: dict[str, Any] | None = None,
            human_readable: bool = False, human_pause: float = 0.18,
            file_resolver: Callable[[dict[str, Any]], str | list[str] | None] | None = None,
            ) -> dict[str, Any]:
    started = time.perf_counter()
    tab = session.tab()
    fields = recording.get("fields") or []
    out: dict[str, Any] = {"ok": False, "stage": "navigate", "planned": len(fields),
                           "filled": 0, "missing": [], "changed": [], "healed": 0, "reason": None,
                           "recording": recording.get("recorded_from")}
    stages = _Stages(out)
    try:
        # Every helper/CDP row written while replaying carries `mode: replay` and the
        # recording it came from, so telemetry can separate replayed work from discovery.
        with session.journal.bind(stage="replay", mode="replay",
                                  recording=recording.get("fingerprint"),
                                  recorded_from=recording.get("recorded_from")):
            tab.goto(url, timeout=timeout, wait_until="DOMContentLoaded", usable_after=None)
            # A form on the landing page (Gem, Ashby: no steps) renders after `load`;
            # resolving before it exists reported "form changed" on every such replay.
            # Same event-driven wait discovery uses, so a rendered form costs nothing.
            if not recording.get("steps"):
                state = _settle(tab, fields, timeout)
                if str(state.get("state")) in ("account_wall", "bot_wall"):
                    out["reason"] = f"landed on {state.get('state')}"
                    stages.enter("wall"); out["stage"] = "wall"
                    return _done(out, started)
            for step in recording.get("steps") or []:
                stages.enter("step:"); out["stage"] = "step:" + step["action"]
                if step["action"] == "goto":
                    tab.goto(step["url"], timeout=timeout, wait_until="DOMContentLoaded", usable_after=None)
                    continue
                ref = None
                row = None
                if step.get("selector"):
                    try:    # event-driven: the control appears long before `load`
                        tab.wait_for(step["selector"], state="present", timeout=min(float(timeout), 8.0))
                    except HarnessError:
                        pass
                deadline = time.perf_counter() + min(float(timeout), 8.0)
                while True:
                    row = _resolve(tab, step.get("selector"))
                    ref = row.get("ref") if row else None
                    if not ref:
                        ref = _find_by_label(tab, step.get("label"))
                        if ref:
                            out["healed"] += 1
                    if ref or time.perf_counter() >= deadline:
                        break
                    time.sleep(0.4)          # an SPA paints its apply button after `load`
                if not ref:
                    # Discovery's scorer, one evaluation: the page changed its markup or
                    # renders the control differently (ZKB, BCG 2026-08-29), but the same
                    # heuristic that recorded it can usually find it again.
                    try:
                        prepared = document.prepare_document(tab, guard_submit=True, timeout=min(float(timeout), 10.0))
                    except HarnessError:
                        prepared = {}
                    control = prepared.get("apply_control") or {}
                    link = str(prepared.get("apply_link") or "")
                    if control.get("ref"):
                        ref = control["ref"]
                        row = {"href": link if link and control.get("is_link") else ""}
                        out["healed"] += 1
                        out["healed_by"] = "scorer"
                    elif link.startswith(("http://", "https://")):
                        ref, row = "apply_link", {"href": link}
                        out["healed"] += 1
                        out["healed_by"] = "scorer_link"
                if not ref:
                    out["reason"] = f"apply control not found: {step.get('label') or step.get('selector')}"
                    return _done(out, started)
                href = str((row or {}).get("href") or "").strip()
                if href and not href.startswith(("javascript:", "#", "mailto:", "tel:")):
                    try:
                        href = urljoin(str(tab.js("location.href") or ""), href)
                    except HarnessError:
                        href = ""
                if href.startswith(("http://", "https://")) and not href.endswith("#"):
                    # A plain link is navigated, not clicked: a synthetic click on a
                    # `target=_blank` link in a hidden tab opens no popup (RUAG, Kanton
                    # Luzern, Brack, ... 2026-08-29), and discovery follows hrefs too.
                    out["navigated"] = href
                    tab.goto(href, timeout=timeout, wait_until="DOMContentLoaded", usable_after=None)
                else:
                    delta = tab.click_ref(ref, timeout=timeout)
                    new_targets = [str(t) for t in (delta.get("new_targets") or []) if t]
                    if new_targets:
                        tab = session.use_tab(new_targets[-1])
                state = _settle(tab, fields, timeout)
                if str(state.get("state")) in ("account_wall", "bot_wall"):
                    out["reason"] = f"landed on {state.get('state')}"
                    stages.enter("wall"); out["stage"] = "wall"
                    return _done(out, started)
            stages.enter("resolve"); out["stage"] = "resolve"
            refs: dict[int, str] = {}
            unresolved: list[int] = []
            for index, f in enumerate(fields):
                row = _resolve(tab, f.get("selector"))
                if row and row.get("ref") and _kind_matches(f.get("kind"), row):
                    refs[index] = row["ref"]
                else:
                    unresolved.append(index)
            if unresolved:
                # Self-heal: one schema read, match the leftovers by label.
                schema = forms.form_schema(tab, timeout=timeout) or {}
                taken = set(refs.values())
                live = [f for f in (schema.get("fields") or []) if f.get("ref") not in taken]
                healed = match_fields_by_label([fields[i] for i in unresolved], live)
                for local_index, ref in healed.items():
                    refs[unresolved[local_index]] = ref
                out["healed"] += len(healed)
                still = [fields[i].get("label") or fields[i].get("selector") for i in unresolved
                         if i not in refs]
                if still:
                    out["missing"] = still
                    out["reason"] = f"form changed: {len(still)} of {len(fields)} recorded fields not found by selector or label"
                    return _done(out, started)
            stages.enter("plan"); out["stage"] = "plan"
            plan, _audit = plan_for_recording(recording, refs, language,
                                              applicant=applicant, profile=profile)
            if not plan:
                out["reason"] = "the planner produced no writes for this applicant"
                return _done(out, started)
            stages.enter("fill"); out["stage"] = "fill"
            outcome = forms.fill_form(tab, plan, timeout=fill_timeout,
                                      human_readable=human_readable, human_pause=human_pause)
            report = outcome.to_json() if hasattr(outcome, "to_json") else {}
            out["fill"] = {k: report.get(k) for k in ("ok", "class", "detail")}
            match = re.search(r"(\d+)/(\d+) succeeded", str(report.get("detail") or ""))
            out["filled"] = int(match.group(1)) if match else (len(plan) if outcome.ok else 0)
            out["planned"] = len(plan)
            if not outcome.ok:
                # Which fields did not take, and would discovery have done better? A field
                # that is optional, a known gap of this recording, or a select with no
                # matching option fails the same way on a fresh discovery — re-discovering
                # for it costs 8-20 s and fills the same fields (measured 2026-08-29:
                # eight "6/9 succeeded" fallbacks, all re-discovered to 6/9).
                unfilled = _unfilled(outcome, refs, fields, recording)
                out["unfilled"] = [u["label"] for u in unfilled]
                blocking = [u for u in unfilled if u["blocking"]]
                if blocking or not unfilled:
                    out["reason"] = f"batched fill: {report.get('detail') or report.get('class')}"
                    stages.enter("partial"); out["stage"] = "partial"
                    return _done(out, started)
                out["partial"] = True
            if file_resolver is not None:
                stages.enter("upload"); out["stage"] = "upload"
                out["uploads"] = _attach(tab, recording.get("files") or [], file_resolver, timeout=timeout)
            if verify:
                stages.enter("verify"); out["stage"] = "verify"
                schema = forms.form_schema(tab, timeout=timeout) or {}
                used = set(refs.values())
                known_gaps = {(u.get("selector") if isinstance(u, dict) else u)
                              for u in (recording.get("required_unplanned") or [])}
                known_labels = {_label_key(u.get("label")) for u in (recording.get("required_unplanned") or [])
                                if isinstance(u, dict)} | {_label_key(f.get("label")) for f in fields}
                known_labels.discard("")
                new_required = [f.get("label") or f.get("selector") for f in schema.get("fields") or []
                                if f.get("required") and f.get("ref") not in used
                                and f.get("selector") not in known_gaps
                                and _label_key(f.get("label")) not in known_labels
                                and not f.get("value") and f.get("kind") not in ("checkbox", "radio", "file")]
                if new_required:
                    out["reason"] = f"new required field(s) not in the recording: {new_required[:3]}"
                    out["changed"] = new_required
                    return _done(out, started)
            out["ok"] = True
            stages.enter("filled"); out["stage"] = "filled"
    except HarnessError as error:
        out["reason"] = f"{error.cls.value}: {str(error)[:120]}"
    return _done(out, started)


def _unfilled(outcome: Any, refs: dict[int, str], fields: list[dict[str, Any]],
              recording: dict[str, Any]) -> list[dict[str, Any]]:
    """The fields the batched fill could not verify, each marked blocking or not."""
    rows = outcome.value if isinstance(getattr(outcome, "value", None), list) else []
    by_ref = {ref: index for index, ref in refs.items()}
    gaps = {_label_key(g.get("label")) for g in (recording.get("required_unplanned") or [])
            if isinstance(g, dict)}
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("ok"):
            continue
        index = by_ref.get(row.get("ref"))
        field = fields[index] if index is not None and index < len(fields) else {}
        error = str(row.get("error") or "")
        label = field.get("label") or row.get("ref") or "?"
        harmless = (not field.get("required")
                    or _label_key(label) in gaps
                    # The batched fill engine is discovery's engine: what it cannot do
                    # here (a select with no matching option, a widget that needs real
                    # interaction) it could not do on a fresh discovery either.
                    or error in ("no_option_match", "no_value_for_toggle", "needs_interaction"))
        out.append({"label": label, "error": error, "required": bool(field.get("required")),
                    "blocking": not harmless})
    return out


class _Stages:
    """Milliseconds per replay stage, so a slow replay says where the time went."""

    def __init__(self, out: dict[str, Any]):
        self.out, self.name, self.t = out, "start", time.perf_counter()
        out["stages"] = {}

    def enter(self, name: str) -> None:
        now = time.perf_counter()
        self.out["stages"][self.name] = round(self.out["stages"].get(self.name, 0) + (now - self.t) * 1000, 1)
        self.name, self.t = name, now


def _done(out: dict[str, Any], started: float) -> dict[str, Any]:
    out["ms"] = round((time.perf_counter() - started) * 1000, 1)
    return out
