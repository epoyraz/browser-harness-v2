"""The job-application workflow, above the harness rather than inside it.

These five entry points were `Session` methods, which made every browser script carry the
opinion that a page might be a job posting. They are judgment about a domain — is this an
application, which control means Apply, has this become an account wall — and judgment is
the model's half of the contract. The harness underneath them stays factual: it reports
controls, labels, options and refs, and executes exactly what it is told.

Nothing in `harness/` imports this package. That is the point: it is what makes a
core-versus-core comparison with v1 meaningful, and it is the seam along which this layer
could move out of the repository entirely.

Each function takes the session it drives as its first argument, which is what a method
was doing implicitly.
"""
from __future__ import annotations

import hashlib
import inspect
import os
import time
from collections.abc import Callable
from typing import Any

from harness.core.outcome import HarnessError
from harness.ops import forms
from harness.skills import Registry as SkillRegistry


def _enabled(value: str | None, *, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in ("", "0", "false", "no", "off")


def _navigation_wait() -> dict[str, Any]:
    """How the application workflow decides a navigation is done.

    `load` is the default because it is the honest answer to "has this page finished".
    But the workflow does not need that answer: `wait_for_application_state` follows every
    navigation, it is event-driven, and it decides the question the caller actually has —
    whether there is a form. Measured on the 2026-08-26 corpus, 69 of 188 readiness checks
    returned in under 500ms, meaning the page was already usable when `goto` handed over,
    and 69 of 99 navigations had waited for `load` to get there.

    Overridable rather than changed outright, so the two can be run against the same corpus
    and compared instead of argued about.
    """
    until = os.environ.get("BH_NAV_WAIT_UNTIL", "").strip() or "load"
    raw = os.environ.get("BH_NAV_USABLE_AFTER", "").strip()
    wait: dict[str, Any] = {"wait_until": until}
    if raw:
        wait["usable_after"] = None if raw.lower() == "none" else float(raw)
    return wait


def _planner_accepts_skill_context(planner: Callable[..., Any]) -> bool:
    """Inspect before calling: catching TypeError would hide a bug inside the planner."""
    try:
        inspect.signature(planner).bind({}, "en", {})
    except (TypeError, ValueError):
        return False
    return True



def prepare_application(session: Any, *, timeout: float = 20.0) -> dict[str, Any]:
    """Guard and inspect the current application, scanning frames only when needed."""
    def is_application(data: dict[str, Any]) -> bool:
        schema = data.get("schema") or {}
        fields = schema.get("fields") or []
        verdict = schema.get("verdict") or {}
        if "is_application" in verdict:
            return bool(verdict.get("is_application"))
        substantial = len(fields) >= 8 or (len(fields) >= 4 and data.get("file_inputs"))
        return bool(verdict.get("is_form") or substantial)

    main = session.tab()
    with session.journal.call("prepare_application"):
        prepared = forms.prepare_document(main, timeout=timeout)
        if is_application(prepared):
            return {**prepared, "target_id": main.target_id, "context": "main",
                    "contexts_checked": 1, "is_application": True}

        candidates = [(main, prepared, "main")]
        for frame in main.frames():
            target_id = frame.get("target_id")
            if not target_id:
                continue
            try:
                frame_tab = session.tab(target_id)
                frame_data = forms.prepare_document(frame_tab, timeout=timeout)
                candidates.append((frame_tab, frame_data, str(frame.get("kind") or "frame")))
            except HarnessError:
                continue
        selected, prepared, context = max(
            candidates,
            key=lambda item: (
                bool(((item[1].get("schema") or {}).get("verdict") or {}).get(
                    "is_application",
                    ((item[1].get("schema") or {}).get("verdict") or {}).get("is_form"),
                )),
                len((item[1].get("schema") or {}).get("fields") or []),
            ),
        )
        session.use_tab(selected.target_id)
        return {**prepared, "target_id": selected.target_id, "context": context,
                "contexts_checked": len(candidates),
                "is_application": is_application(prepared)}


def follow_application(session: Any, prepared: dict[str, Any], *, timeout: float = 15.0,
                       settle: float = 0.25,
                       candidates: list[str] | None = None) -> dict[str, Any]:
    """Advance from a posting to its application UI and report the chosen target.

    The transition may replace the current document, reveal an in-page form, expose a
    discovered URL, or create a new browser target.  Callers should not have to encode
    those four shapes independently, and a new target must become current before the
    next perception call or the application is inspected in the wrong tab.
    """
    origin = session.tab()
    control = prepared.get("apply_control") or {}
    link = prepared.get("apply_link")
    current_url = prepared.get("url")
    candidate = next((url for url in (candidates or []) if url != current_url), None)
    transition: dict[str, Any] = {"kind": "none"}
    selected = origin

    with session.journal.call("follow_application"):
        if control.get("ref"):
            delta = origin.click_ref(control["ref"], settle=settle, timeout=timeout)
            transition = {"kind": "control", "delta": delta}
            new_targets = [str(t) for t in delta.get("new_targets") or [] if t]
            if new_targets:
                selected = session.use_tab(new_targets[-1])
                transition["kind"] = "new_target"
                transition["target_id"] = selected.target_id
            elif (not delta.get("navigated") and not delta.get("dom_mutations")
                  and ((link and link != current_url) or candidate)):
                destination = str(link or candidate)
                navigation = origin.goto(destination, timeout=timeout, **_navigation_wait())
                transition = {"kind": ("fallback_link" if link else "candidate_link"),
                              "delta": delta,
                              "navigation": navigation}
        elif link and link != current_url:
            navigation = origin.goto(str(link), timeout=timeout, **_navigation_wait())
            transition = {"kind": "link", "navigation": navigation}
        elif candidate:
            navigation = origin.goto(candidate, timeout=timeout, **_navigation_wait())
            transition = {"kind": "candidate_link", "navigation": navigation}

        state = selected.wait_for_application_state(timeout=timeout)
        session.use_tab(selected.target_id)
    return {"transition": transition, "state": state,
            "target_id": selected.target_id,
            "target_changed": selected.target_id != origin.target_id}


def locate_application(session: Any, url: str, *, timeout: float = 25.0,
                       transition_timeout: float = 15.0,
                       hop_budget: int = 6,
                       candidates: list[str] | None = None) -> dict[str, Any]:
    """Navigate to and locate an application UI without encoding ATS-specific steps.

    Every transition shape is handled by :meth:`follow_application`.  The state
    returned by that transition is reused by the following hop, avoiding the duplicate
    wait that dominated the 100-job trace.  A structural fingerprint stops cycles;
    ``hop_budget`` is only the final safety ceiling, not the workflow definition.
    The final form verdict wins over a stale ``usable_ui``/``account_wall`` probe and
    the disagreement is retained as evidence.
    """
    if hop_budget < 1:
        raise ValueError("hop_budget must be positive")
    started = time.perf_counter()
    hops: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    route_candidates = list(candidates or forms.application_route_candidates(url))
    with session.journal.bind(stage="navigate"):
        navigation = session.tab().goto(url, timeout=timeout, **_navigation_wait())
    pending_state: dict[str, Any] | None = None
    prepared: dict[str, Any] = {}
    terminal = "budget_exhausted"

    for hop in range(hop_budget):
        with session.journal.bind(stage="inspect", hop=hop):
            state = pending_state or session.tab().wait_for_application_state(
                timeout=min(timeout, 12.0))
            pending_state = None
            prepared = prepare_application(session, timeout=timeout)
        verdict = (prepared.get("schema") or {}).get("verdict") or {}
        is_application = bool(prepared.get("is_application"))
        observed_state = str(state.get("state") or "stable_failure")
        reconciled = "form" if is_application else observed_state
        fingerprint = (
            prepared.get("target_id"), prepared.get("url"), reconciled,
            verdict.get("fields"), prepared.get("apply_link"),
            (prepared.get("apply_control") or {}).get("ref"),
        )
        row = {
            "hop": hop, "url": prepared.get("url"), "title": prepared.get("title"),
            "target_id": prepared.get("target_id"),
            "is_application": is_application, "context": prepared.get("context"),
            "contexts_checked": prepared.get("contexts_checked"),
            "apply_link": prepared.get("apply_link"),
            "apply_control": prepared.get("apply_control"),
            "application_urls": prepared.get("application_urls") or [],
            "application_state": state, "reconciled_state": reconciled,
            "state_conflict": is_application and observed_state != "form",
            "verdict": verdict,
        }
        hops.append(row)
        if is_application:
            terminal = "form"
            break
        if fingerprint in seen:
            terminal = "cycle"
            break
        seen.add(fingerprint)
        for discovered in prepared.get("application_urls") or []:
            if discovered != prepared.get("url") and discovered not in route_candidates:
                route_candidates.append(discovered)
        if not (prepared.get("apply_control") or prepared.get("apply_link")
                or route_candidates):
            terminal = reconciled
            break
        with session.journal.bind(stage="transition", hop=hop):
            followed = follow_application(session, 
                prepared, timeout=transition_timeout, candidates=route_candidates)
        if followed["transition"].get("kind") == "candidate_link":
            route_candidates = []
        row["transition"] = followed["transition"]
        row["transition_state"] = followed["state"]
        row["target_id_after"] = followed["target_id"]
        row["target_changed"] = followed["target_changed"]
        pending_state = followed["state"]

    return {
        "navigation": navigation, "hops": hops, "prepared": prepared,
        "terminal_state": terminal,
        "wall_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def run_application(session: Any, url: str, *,
                    planner: Callable[..., Any] | None = None,
                    timeout: float = 25.0, transition_timeout: float = 15.0,
                    fill_timeout: float = 30.0, hop_budget: int = 6,
                    candidates: list[str] | None = None,
                    skills: bool | None = None,
                    human_readable: bool = False,
                    human_pause: float = 0.18) -> dict[str, Any]:
    """Locate and optionally fill an application as one typed, non-submitting flow.

    ``planner`` receives the final schema and language. A planner accepting a third
    positional argument additionally receives the matched, digest-verified skill
    packet, including the exact delimited ``model_context``. It may return a plan or
    ``(plan, audit)``. Two-argument planners remain unchanged. The browser's dry-run
    boundary remains the authority: this
    workflow has no submit operation and cannot weaken the guard. Pass
    ``human_readable=True`` to smoothly reveal and fill one field at a time for a
    recording; the default keeps the fast batched path.
    """
    skill_context = application_skills(session, url, enabled=skills)
    located = locate_application(session, 
        url, timeout=timeout, transition_timeout=transition_timeout,
        hop_budget=hop_budget, candidates=candidates)
    prepared = located["prepared"]
    routed_urls = [url]
    routed_urls.extend(str(hop.get("url") or "") for hop in located.get("hops") or [])
    routed_urls.append(str(prepared.get("url") or ""))
    skill_context = application_skills(session, *routed_urls, enabled=skills)
    result: dict[str, Any] = {
        "stage": located["terminal_state"], "location": located,
        "skills": skill_context, "plan": [], "audit": [], "fill": None,
    }
    if not prepared.get("is_application") or planner is None:
        return result
    planner_args = (prepared.get("schema") or {},
                    str(prepared.get("language") or "en"))
    planned = (planner(*planner_args, skill_context)
               if _planner_accepts_skill_context(planner) else planner(*planner_args))
    if isinstance(planned, tuple) and len(planned) == 2:
        plan, audit = planned
    else:
        plan, audit = planned, []
    result["plan"] = list(plan or [])
    result["audit"] = list(audit or [])
    started = time.perf_counter()
    with session.journal.bind(stage="fill"):
        outcome = forms.fill_form(
            session.tab(), result["plan"], timeout=fill_timeout,
            human_readable=human_readable, human_pause=human_pause)
    result["fill"] = outcome.to_json()
    result["fill_ms"] = round((time.perf_counter() - started) * 1000, 1)
    result["stage"] = "filled" if outcome.ok else "partial"
    return result


def application_skills(session: Any, *urls: str, enabled: bool | None = None) -> dict[str, Any]:
    """Resolve and digest-verify model context for application URLs, with zero CDP.

    Public bodies remain explicitly delimited untrusted reference material. Paths are
    intentionally omitted from the planner packet: provenance is source/id/version and
    digest, not a machine-local cache location.
    """
    active = (_enabled(os.environ.get("BH_APPLICATION_SKILLS"))
              if enabled is None else bool(enabled))
    if not active:
        return {"enabled": False, "matches": [], "model_context": "",
                "bytes": 0, "sha256": None}
    if session._skill_registry is None:
        session._skill_registry = SkillRegistry()
    found: dict[tuple[str, str, str], dict[str, Any]] = {}
    for url in dict.fromkeys(str(item) for item in urls if item):
        for ref in session._skill_registry.match(url):
            key = (ref.source, ref.id, ref.version)
            record = found.setdefault(key, {"ref": ref, "matched_urls": []})
            record["matched_urls"].append(url)
    matches = []
    bodies = []
    for record in found.values():
        ref = record["ref"]
        body = session._skill_registry.load(ref)
        metadata = ref.to_json()
        metadata.pop("path", None)
        metadata["matched_urls"] = record["matched_urls"]
        matches.append(metadata)
        bodies.append(body.for_model())
    model_context = "\n\n".join(bodies)
    packet = {
        "enabled": True, "matches": matches, "model_context": model_context,
        "bytes": len(model_context.encode("utf-8")),
        "sha256": hashlib.sha256(model_context.encode("utf-8")).hexdigest()
        if model_context else None,
    }
    session.journal.write("note", event="application_skills_resolved",
                       matched=len(matches), ids=[item["id"] for item in matches],
                       bytes=packet["bytes"], sha256=packet["sha256"])
    return packet

