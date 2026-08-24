"""The agent-facing entry point: `Session` and the namespace a script runs in.

Everything below the surface — daemon, registry, isolated worlds, typed outcomes — exists
so that this file can be small. A script gets working helpers bound to a tab and never
assembles a connection, a registry and a `Tab` by hand.

**A current tab is client-local, deliberately.** v1's daemon held a shared `current_tab`,
so two subagents fought over one browser (#375). Here the *daemon* still keeps no cursor —
every request names its target — while each client process keeps its own. Two scripts can
run against two tabs concurrently and neither can steal the other's, which is the property
D1 was after; the ergonomic convenience of "there is a current tab" was never the problem.
"""
from __future__ import annotations

import hashlib
import inspect
import os
import threading
import time
from collections.abc import Callable
from typing import Any, Self

from harness import extend
from harness.auth import account_credential_status, ensure_account_credential
from harness.connect.client import RemoteConnection, RemoteRegistry, ensure_daemon
from harness.core.content import (
    DEFAULT_OUTPUT_BYTES,
    DEFAULT_VALUE_BYTES,
    ContentStore,
    OutputCapture,
)
from harness.core.journal import Journal
from harness.core.outcome import Class, HarnessError, ScopeRefused, TargetGone
from harness.ops import batch, forms, record, screencast
from harness.ops import parallel as parallel_ops
from harness.ops.page import Tab
from harness.skills import Registry as SkillRegistry

#: A tab we may drive. `about:blank` counts; chrome:// internals and devtools do not.
_DRIVABLE = ("http://", "https://", "file://", "about:blank", "data:")


def _enabled(value: str | None, *, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in ("", "0", "false", "no", "off")


def _planner_accepts_skill_context(planner: Callable[..., Any]) -> bool:
    """Inspect before calling: catching TypeError would hide a bug inside the planner."""
    try:
        inspect.signature(planner).bind({}, "en", {})
    except (TypeError, ValueError):
        return False
    return True


class Session:
    """One client's view of the browser: a daemon connection and a current tab."""

    def __init__(self, name: str = "default", *, journal_path: str | None = None,
                 accept_dialogs: bool = False,
                 content_store: ContentStore | None = None):
        self.name = name
        ensure_daemon(name)
        self.content_store = content_store or ContentStore()
        try:
            self._value_limit = max(0, int(os.environ.get(
                "BH_OUTPUT_BYTES", str(DEFAULT_VALUE_BYTES))))
        except ValueError:
            self._value_limit = DEFAULT_VALUE_BYTES
        self.journal = Journal(
            journal_path or os.environ.get("BH_JOURNAL") or None,
            session=name, content_store=self.content_store,
            entry_limit=self._value_limit,
        )
        self._output_lock = threading.Lock()
        self._output_elisions = 0
        self._output_spilled_bytes = 0
        self.conn = RemoteConnection(name, journal=self.journal)
        self.registry = RemoteRegistry(self.conn)
        self.accept_dialogs = accept_dialogs
        self._tabs: dict[str, Tab] = {}
        self._tabs_lock = threading.Lock()
        self._attach_locks: dict[str, threading.Lock] = {}
        self._contexts: set[str] = set()
        self._tab_context: dict[str, str] = {}
        # The current tab is per-thread as well as per-client. Client-local already stops
        # two *processes* fighting over one cursor (D1, v1 #375); making it thread-local
        # extends the same property to `parallel()`, where N workers drive N tabs inside
        # one process. A shared cursor there would mean worker A's goto() silently
        # redirecting worker B's next js() — the exact bug D1 exists to prevent, moved
        # one level in.
        self._local = threading.local()
        self._recorder: record.Recorder | None = None
        self._screencast: screencast.ScreencastRecorder | None = None
        self._skill_registry: SkillRegistry | None = None
        self.extensions: list[dict[str, Any]] = []
        if lease := os.environ.get("BH_TARGET_LEASE"):
            # An explicit lease takes precedence over the ergonomic first-tab fallback.
            # If it cannot be claimed, fail closed instead of driving whatever happens to
            # be first in Chrome after this fresh process connects.
            self.resume_lease(lease)
        recording = os.environ.get("BH_RECORD", "").strip().lower()
        if recording not in ("", "0", "false", "no", "off"):
            # A profile name is also a convenient one-variable opt-in.  Legacy truthy
            # values (especially BH_RECORD=1) retain the review profile unless the
            # dedicated profile variable says otherwise.
            selected = recording if recording in {profile.value for profile in record.Profile} \
                else None
            self.start_recording(profile=selected)

    # -- tabs --------------------------------------------------------------

    def targets(self) -> list[dict[str, Any]]:
        infos = self.conn.request("Target.getTargets")["targetInfos"]
        return [t for t in infos if t.get("type") == "page"]

    def drivable(self) -> list[dict[str, Any]]:
        return [t for t in self.targets() if str(t.get("url", "")).startswith(_DRIVABLE)]

    @property
    def _current(self) -> str | None:
        return getattr(self._local, "current", None)

    @_current.setter
    def _current(self, value: str | None) -> None:
        self._local.current = value

    def _attach(self, tid: str) -> Tab:
        with self._tabs_lock:
            attach_lock = self._attach_locks.setdefault(tid, threading.Lock())
        # Per-target rather than global: different tabs still attach concurrently, while
        # two workers racing the same target cannot both construct a Tab and install its
        # isolated-world runtime twice.
        with attach_lock:
            with self._tabs_lock:
                tab = self._tabs.get(tid)
            if tab is None:
                tab = Tab(self.conn, self.registry, tid, journal=self.journal,
                          accept_dialogs=self.accept_dialogs,
                          content_store=self.content_store)
                with self._tabs_lock:
                    self._tabs[tid] = tab
        previous, self._current = self._current, tid
        if previous != tid and _enabled(os.environ.get("BH_TAB_MARK"), default=False):
            self._move_tab_marker(tab, previous)
        return tab

    #: The driven-tab marker (🐴, browser-use's convention). A human watching ten hidden
    #: worker tabs fill forms has no way to tell the harness's tabs from their own; the
    #: title prefix is that answer, one glance at the tab strip. OPT-IN via BH_TAB_MARK,
    #: default off, because `document.title` is page-visible state — analytics read it,
    #: and the detectability contract is that the harness announces nothing to the page
    #: unless the operator asks it to.
    _MARK = "\U0001f434 "

    def _move_tab_marker(self, tab: Tab, previous: str | None) -> None:
        """Mark the newly current tab; unmark the one this thread just left.

        Best-effort on both sides: a tab mid-navigation or already closing must never
        turn a cursor move into a failure. The marker is 3 UTF-16 units (surrogate pair
        plus space), hence slice(3). It survives until the page rewrites its own title —
        an SPA that does is unmarked until the next cursor move, which is acceptable for
        a purely cosmetic aid.
        """
        try:
            tab._world_js(
                "(() => { if (!document.title.startsWith('\\ud83d\\udc34 '))"
                " document.title = '\\ud83d\\udc34 ' + document.title; return true; })()",
                timeout=5.0)
        except HarnessError:
            pass
        if previous is None:
            return
        with self._tabs_lock:
            old = self._tabs.get(previous)
        if old is None:
            return
        try:
            old._world_js(
                "(() => { if (document.title.startsWith('\\ud83d\\udc34 '))"
                " document.title = document.title.slice(3); return true; })()",
                timeout=5.0)
        except HarnessError:
            pass

    def tab(self, target_id: str | None = None) -> Tab:
        """The current tab, or a named one. Attaches to a real page if we have none yet —
        and creates one rather than seizing a `chrome://` internal, which is not a page a
        caller ever meant.

        The no-target fallback is decided by the DAEMON (`adopt`), not by scanning here:
        computed client-side, two fresh clients against one browser both took the same
        first drivable page and clobbered each other's navigations — the multi-client
        collision browser-use PR 618 fixes in v1. The daemon hands each connected client
        the first page nobody else has adopted, or a fresh background tab, atomically;
        adoption dies with the client's connection and never restricts an explicit
        `use_tab(target_id)`.

        `getTargets` happily lists a tab that is already closing, and `parallel()` closes
        a whole worker pool's tabs at once — so an adopted target can be dead by the time
        the attach lands; the retry excludes it and asks again. A daemon predating the
        meta gets the legacy client-side scan, so a stale long-lived daemon degrades to
        the old behaviour instead of refusing to hand out a tab.
        """
        tid = target_id or self._current
        if tid is not None:
            return self._attach(tid)
        exclude: list[str] = []
        last: HarnessError | None = None
        for _ in range(4):
            try:
                adopted = self.conn.adopt_default_target(exclude=exclude)
            except HarnessError as error:
                if "unknown meta" not in str(error):
                    raise
                return self._legacy_default_tab()
            try:
                return self._attach(adopted["target_id"])
            except HarnessError as error:
                last = error
                exclude.append(adopted["target_id"])
        raise last if last is not None else TargetGone("no adoptable tab could be attached")

    def _legacy_default_tab(self) -> Tab:
        """The pre-adoption fallback, kept verbatim for daemons that predate `adopt`."""
        for page in self.drivable():
            try:
                return self._attach(page["targetId"])
            except HarnessError:
                continue          # closing, or gone between the listing and the attach
        return self._attach(self.conn.request(
            "Target.createTarget",
            {"url": "about:blank", "background": True})["targetId"])

    def new_context(self) -> str:
        """Create an owned incognito browser context for cookie/storage isolation."""
        context_id = self.conn.request("Target.createBrowserContext")["browserContextId"]
        with self._tabs_lock:
            self._contexts.add(context_id)
        self.journal.write("note", event="resource_acquired", resource_kind="browser_context",
                           identifier=context_id)
        return context_id

    def close_context(self, context_id: str) -> None:
        """Dispose only a context this session created, including all of its tabs."""
        with self._tabs_lock:
            if context_id not in self._contexts:
                raise ScopeRefused("refusing to dispose an unowned browser context",
                                   context_id=context_id)
            target_ids = [target_id for target_id, owned in self._tab_context.items()
                          if owned == context_id]
        self.conn.request("Target.disposeBrowserContext", {"browserContextId": context_id})
        with self._tabs_lock:
            self._contexts.remove(context_id)
            for target_id in target_ids:
                self._tab_context.pop(target_id, None)
                tab = self._tabs.pop(target_id, None)
                if tab is not None:
                    tab.close()
                self.registry.forget(target_id)
        if self._current in target_ids:
            self._current = None
        self.journal.write("note", event="resource_released", resource_kind="browser_context",
                           identifier=context_id)

    def new_tab(self, url: str = "about:blank", *, context_id: str | None = None) -> Tab:
        """Create, attach, and make current. Always `about:blank` first, then navigate:
        passing a url to `createTarget` races the attach, so the brief blank page reads as
        'complete' and a wait returns before the real navigation starts (v1's comment).

        Created in the BACKGROUND, deliberately. `Target.createTarget` defaults to
        foreground, and measured on four consecutive creations that meant: the user's
        selected tab loses focus once per tab, and afterwards exactly one harness tab —
        whichever was created LAST — is the window's selected tab and can receive raw
        Input.* events, while the rest silently drop them. A ten-worker run stole the
        user's focus ten times and left the one input-capable tab to a lottery.
        Background creation removes both: the user's tab stays put, and every worker tab
        is in the same, deterministic state — hidden — which the input paths handle by
        verifying delivery and falling back through the DOM. `activate_tab()` is the
        explicit opt-in for the page that genuinely needs visibility.
        """
        params = {"url": "about:blank", "background": True}
        if context_id is not None:
            with self._tabs_lock:
                if context_id not in self._contexts:
                    raise ScopeRefused("refusing to create a tab in an unowned context",
                                       context_id=context_id)
            params["browserContextId"] = context_id
        tid = self.conn.request("Target.createTarget", params)["targetId"]
        if context_id is not None:
            with self._tabs_lock:
                self._tab_context[tid] = context_id
        try:
            tab = self.tab(tid)
            if url and url != "about:blank":
                tab.goto(url)
            return tab
        except Exception:
            self.journal.write("note", event="resource_rollback", resource_kind="tab",
                               identifier=tid)
            self.close_tab(tid)
            raise

    def use_tab(self, target_id: str) -> Tab:
        return self.tab(target_id)

    def activate_tab(self, target_id: str | None = None) -> str:
        """Bring a tab to the front of its window — the explicit opt-in for visibility.

        Everything else works hidden: screenshots, evaluation, batched fills, and — via
        delivery-verified fallbacks — clicks, keystrokes and scrolling. What activation
        buys is the renderer's raw input path and unthrottled rendering, so it exists for
        exactly two callers: a page that demonstrably pauses visibility-dependent work
        while hidden, and a human who wants to watch. It is never called implicitly —
        ten parallel workers activating would fight over one window's selected slot.
        """
        tab = self.tab(target_id)
        self.conn.request("Target.activateTarget", {"targetId": tab.target_id})
        self.journal.write("note", event="tab_activated", target_id=tab.target_id)
        return tab.target_id

    def lease_tab(self, target_id: str | None = None) -> str:
        """Reserve one tab for later fresh clients and return its opaque lease token.

        Pass that token as ``BH_TARGET_LEASE`` to a later ``bh`` invocation.  The daemon
        owns the target mapping; callers never persist a raw target id and stale leases
        fail closed rather than selecting another browser tab.
        """
        target = self.tab(target_id)
        lease = self.conn.create_target_lease(target.target_id)
        self.journal.write("note", event="target_lease_created", target_id=target.target_id)
        return lease

    def resume_lease(self, lease: str) -> Tab:
        """Make a daemon-owned lease's target current for this client."""
        target_id = self.conn.claim_target_lease(lease)
        tab = self._attach(target_id)
        self.journal.write("note", event="target_lease_claimed", target_id=target_id)
        return tab

    def release_lease(self, lease: str) -> None:
        self.conn.release_target_lease(lease)
        self.journal.write("note", event="target_lease_released")

    def close_tab(self, target_id: str | None = None, *, wait: bool = True) -> None:
        tid = target_id or self._current
        if tid is None:
            return
        # Close FIRST, while this client's Tab is still subscribed. Closing a tab whose
        # page armed beforeunload makes Chrome raise its "Leave site?" dialog, and the
        # Tab's dialog auto-resolver is the thing that answers it — the local teardown
        # used to run first, which unsubscribed the only listener, left the dialog
        # unanswered, and blocked Target.closeTarget behind it. That is the parallel()
        # cleanup path: every reuse_tabs=False item and every end-of-run worker-tab
        # release closes a tab that was just FILLED, which is exactly the page that
        # armed the handler.
        def teardown() -> None:
            with self._tabs_lock:
                tab = self._tabs.pop(tid, None)
                self._tab_context.pop(tid, None)
            if tab is not None:
                tab.close()
            self.registry.forget(tid)
            if self._current == tid:
                self._current = None
        try:
            self.conn.request("Target.closeTarget", {"targetId": tid})
        except HarnessError as error:
            if error.cls not in (Class.TARGET_GONE, Class.SESSION_STALE):
                teardown()
                self.journal.write("note", event="resource_cleanup_failed",
                                   resource_kind="tab", identifier=tid,
                                   error=str(error)[:200])
                raise
        teardown()
        # Chrome acknowledges closeTarget before the target always disappears from
        # getTargets. Clean-tab workers must not open their replacement during that gap,
        # or a declared three-tab run briefly becomes four renderers on a memory-bound Mac.
        if not wait:
            return
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            infos = self.conn.request("Target.getTargets").get("targetInfos") or []
            if not any(info.get("targetId") == tid for info in infos):
                break
            time.sleep(0.01)

    def prepare_application(self, *, timeout: float = 20.0) -> dict[str, Any]:
        """Guard and inspect the current application, scanning frames only when needed."""
        def is_application(data: dict[str, Any]) -> bool:
            schema = data.get("schema") or {}
            fields = schema.get("fields") or []
            verdict = schema.get("verdict") or {}
            if "is_application" in verdict:
                return bool(verdict.get("is_application"))
            substantial = len(fields) >= 8 or (len(fields) >= 4 and data.get("file_inputs"))
            return bool(verdict.get("is_form") or substantial)

        main = self.tab()
        with self.journal.call("prepare_application"):
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
                    frame_tab = self.tab(target_id)
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
            self.use_tab(selected.target_id)
            return {**prepared, "target_id": selected.target_id, "context": context,
                    "contexts_checked": len(candidates),
                    "is_application": is_application(prepared)}

    def follow_application(self, prepared: dict[str, Any], *, timeout: float = 15.0,
                           settle: float = 0.25,
                           candidates: list[str] | None = None) -> dict[str, Any]:
        """Advance from a posting to its application UI and report the chosen target.

        The transition may replace the current document, reveal an in-page form, expose a
        discovered URL, or create a new browser target.  Callers should not have to encode
        those four shapes independently, and a new target must become current before the
        next perception call or the application is inspected in the wrong tab.
        """
        origin = self.tab()
        control = prepared.get("apply_control") or {}
        link = prepared.get("apply_link")
        current_url = prepared.get("url")
        candidate = next((url for url in (candidates or []) if url != current_url), None)
        transition: dict[str, Any] = {"kind": "none"}
        selected = origin

        with self.journal.call("follow_application"):
            if control.get("ref"):
                delta = origin.click_ref(control["ref"], settle=settle, timeout=timeout)
                transition = {"kind": "control", "delta": delta}
                new_targets = [str(t) for t in delta.get("new_targets") or [] if t]
                if new_targets:
                    selected = self.use_tab(new_targets[-1])
                    transition["kind"] = "new_target"
                    transition["target_id"] = selected.target_id
                elif (not delta.get("navigated") and not delta.get("dom_mutations")
                      and ((link and link != current_url) or candidate)):
                    destination = str(link or candidate)
                    navigation = origin.goto(destination, timeout=timeout)
                    transition = {"kind": ("fallback_link" if link else "candidate_link"),
                                  "delta": delta,
                                  "navigation": navigation}
            elif link and link != current_url:
                navigation = origin.goto(str(link), timeout=timeout)
                transition = {"kind": "link", "navigation": navigation}
            elif candidate:
                navigation = origin.goto(candidate, timeout=timeout)
                transition = {"kind": "candidate_link", "navigation": navigation}

            state = selected.wait_for_application_state(timeout=timeout)
            self.use_tab(selected.target_id)
        return {"transition": transition, "state": state,
                "target_id": selected.target_id,
                "target_changed": selected.target_id != origin.target_id}

    def locate_application(self, url: str, *, timeout: float = 25.0,
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
        with self.journal.bind(stage="navigate"):
            navigation = self.tab().goto(url, timeout=timeout)
        pending_state: dict[str, Any] | None = None
        prepared: dict[str, Any] = {}
        terminal = "budget_exhausted"

        for hop in range(hop_budget):
            with self.journal.bind(stage="inspect", hop=hop):
                state = pending_state or self.tab().wait_for_application_state(
                    timeout=min(timeout, 12.0))
                pending_state = None
                prepared = self.prepare_application(timeout=timeout)
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
            with self.journal.bind(stage="transition", hop=hop):
                followed = self.follow_application(
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

    def application_skills(self, *urls: str, enabled: bool | None = None) -> dict[str, Any]:
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
        if self._skill_registry is None:
            self._skill_registry = SkillRegistry()
        found: dict[tuple[str, str, str], dict[str, Any]] = {}
        for url in dict.fromkeys(str(item) for item in urls if item):
            for ref in self._skill_registry.match(url):
                key = (ref.source, ref.id, ref.version)
                record = found.setdefault(key, {"ref": ref, "matched_urls": []})
                record["matched_urls"].append(url)
        matches = []
        bodies = []
        for record in found.values():
            ref = record["ref"]
            body = self._skill_registry.load(ref)
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
        self.journal.write("note", event="application_skills_resolved",
                           matched=len(matches), ids=[item["id"] for item in matches],
                           bytes=packet["bytes"], sha256=packet["sha256"])
        return packet

    def run_application(self, url: str, *,
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
        skill_context = self.application_skills(url, enabled=skills)
        located = self.locate_application(
            url, timeout=timeout, transition_timeout=transition_timeout,
            hop_budget=hop_budget, candidates=candidates)
        prepared = located["prepared"]
        routed_urls = [url]
        routed_urls.extend(str(hop.get("url") or "") for hop in located.get("hops") or [])
        routed_urls.append(str(prepared.get("url") or ""))
        skill_context = self.application_skills(*routed_urls, enabled=skills)
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
        with self.journal.bind(stage="fill"):
            outcome = forms.fill_form(
                self.tab(), result["plan"], timeout=fill_timeout,
                human_readable=human_readable, human_pause=human_pause)
        result["fill"] = outcome.to_json()
        result["fill_ms"] = round((time.perf_counter() - started) * 1000, 1)
        result["stage"] = "filled" if outcome.ok else "partial"
        return result

    # -- recording ---------------------------------------------------------

    def start_recording(self, name: str | None = None, title: str | None = None, *,
                        profile: str | record.Profile | None = None) -> str:
        """Record action evidence under an explicit evidence/review/cinematic profile.

        Turning it on *moves* the journal into the recording directory: the frames and the
        calls that produced them become one artifact instead of two to correlate.
        """
        if self._recorder is not None:
            if profile is not None and record.parse_profile(profile) is not self._recorder.profile:
                raise ValueError(
                    f"recording already uses profile {self._recorder.profile.value!r}")
            return str(self._recorder.dir)
        selected = record.parse_profile(
            profile if profile is not None else os.environ.get("BH_RECORD_PROFILE"))
        self._recorder = record.start(lambda: self._current and self._tabs.get(self._current),
                                      self.journal, name=name, title=title, profile=selected)
        # Batch evidence runs may deliberately create more than the interactive default
        # of 20 recordings. Keep rollover bounded while allowing the caller to preserve
        # the whole batch explicitly.
        keep = max(1, int(os.environ.get("BH_RECORDING_KEEP", "20")))
        record.prune(keep=keep)
        return str(self._recorder.dir)

    def stop_recording(self) -> str | None:
        if self._recorder is None:
            return None
        directory = self._recorder.stop()
        self._recorder = None
        return str(directory)

    def start_screencast(self, name: str | None = None, *, quality: int = 88,
                         max_width: int = 1440, max_height: int = 1000,
                         every_nth_frame: int = 1) -> str:
        """Continuously capture compositor updates for the current tab through CDP."""
        if self._screencast is not None:
            return str(self._screencast.dir)
        self._screencast = screencast.start(
            self.tab(), name=name, quality=quality, max_width=max_width,
            max_height=max_height, every_nth_frame=every_nth_frame)
        return str(self._screencast.dir)

    def stop_screencast(self) -> str | None:
        if self._screencast is None:
            return None
        directory = self._screencast.stop()
        self._screencast = None
        return str(directory)

    def close(self) -> None:
        self.stop_screencast()
        self.stop_recording()
        with self._tabs_lock:
            contexts = list(self._contexts)
        for context_id in contexts:
            try:
                self.close_context(context_id)
            except HarnessError as error:
                self.journal.write("note", event="resource_cleanup_failed",
                                   resource_kind="browser_context", identifier=context_id,
                                   error=str(error)[:200])
        with self._tabs_lock:
            tabs, self._tabs = list(self._tabs.values()), {}
        for tab in tabs:
            tab.close()
        self.conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        self.close()
        return False

    # -- the script namespace ---------------------------------------------

    def namespace(self) -> dict[str, Any]:
        """What a `bh` script sees. Helpers are late-bound to the *current* tab, so
        `use_tab()` mid-script redirects them — a script reads top to bottom."""
        def on_tab(fn_name: str):
            def call(*a: Any, **kw: Any) -> Any:
                value = getattr(self.tab(), fn_name)(*a, **kw)
                return self._bound_agent_value(fn_name, value)
            call.__name__ = fn_name
            return call

        def with_tab(fn):
            def call(*a: Any, **kw: Any) -> Any:
                value = fn(self.tab(), *a, **kw)
                return self._bound_agent_value(fn.__name__, value)
            call.__name__ = fn.__name__
            return call

        def on_session(fn_name: str):
            def call(*a: Any, **kw: Any) -> Any:
                value = getattr(self, fn_name)(*a, **kw)
                return self._bound_agent_value(fn_name, value)
            call.__name__ = fn_name
            return call

        def parallel(items, fn, **kw):
            """Keep aggregate records composable until the stdout boundary.

            ``parallel()`` is intentionally paired with ``summarise(records)`` and with
            artifact writers. Replacing a large record list by an output marker here
            destroys that contract before the script emits anything. Individual helper
            results remain bounded, and the invocation-wide stdout capture still prevents
            the aggregate from flooding the agent transcript if it is printed.
            """
            return parallel_ops.parallel(self, items, fn, **kw)

        def open_pages(urls, *, workers: int = 5, total_chars: int = 12_000,
                       max_links: int = 12, page_timeout: float = 20.0,
                       timeout: float | None = 120.0):
            """Read independent public pages concurrently under one total text budget.

            ``open_page``'s character limit is per page, an easy way for a five-URL loop to
            print five model windows. This convenience makes the intended fast path both
            shorter to write and bounded across the whole batch.
            """
            items = [str(url) for url in urls]
            if not items:
                return []
            per_page = max(0, min(100_000, int(total_chars) // len(items)))

            def inspect(url: str) -> dict[str, Any]:
                result = self.tab().open_page(
                    url,
                    timeout=page_timeout,
                    max_chars=per_page,
                    max_links=max_links,
                )
                page = result["page"]
                return {
                    "url": result["landed"],
                    "lifecycle": result["lifecycle"],
                    "title": page["title"],
                    "text": page["text"],
                    "links": page["links"],
                    "truncated": page["text_truncated"],
                    "challenge": page["challenge"],
                }

            result = parallel_ops.parallel(
                self,
                items,
                inspect,
                workers=workers,
                isolated=False,
                timeout=timeout,
            )
            return self._bound_agent_value("open_pages", result)

        ns: dict[str, Any] = {
            "session": self, "tab": self.tab, "new_tab": self.new_tab,
            "use_tab": self.use_tab, "close_tab": self.close_tab,
            "activate_tab": self.activate_tab,
            "lease_tab": self.lease_tab, "resume_lease": self.resume_lease,
            "release_lease": self.release_lease,
            "new_context": self.new_context, "close_context": self.close_context,
            "targets": on_session("targets"), "journal": self.journal,
            "form_schema": with_tab(forms.form_schema),
            "fill_form": with_tab(forms.fill_form),
            "set_value": with_tab(forms.set_value),
            "set_secret_from_keychain": with_tab(forms.set_secret_from_keychain),
            "select_option": with_tab(forms.select_option),
            "require_form": forms.require_form,
            "fetch_all": with_tab(batch.fetch_all),
            "fetch_observed_json": with_tab(batch.fetch_observed_json),
            "open_pages": open_pages,
            "application_route_candidates": forms.application_route_candidates,
            "account_credential_status": account_credential_status,
            "ensure_account_credential": ensure_account_credential,
            "prepare_application": on_session("prepare_application"),
            "follow_application": on_session("follow_application"),
            "locate_application": on_session("locate_application"),
            "application_skills": on_session("application_skills"),
            "run_application": lambda *a, **kw: self._bound_agent_value(
                "run_application", self.run_application(*a, **kw)),
            # Bound to this session, so a script writes parallel(urls, fn) and the bare
            # helpers inside fn address that worker's own tab.
            "parallel": parallel,
            "summarise": parallel_ops.summarise,
            "CancelToken": parallel_ops.CancelToken,
        }
        for name in ("goto", "open_page", "read_page", "js", "cdp", "snapshot", "see",
                     "click_ref", "click_at",
                     "click_auth_ref",
                     "capture_screenshot", "wait_lifecycle", "wait_for", "wait_for_form",
                     "wait_for_application_state",
                     "start_diagnostics", "diagnostics",
                     "frames",
                     "page_text", "press_key", "type_chars", "scroll", "upload_file",
                     "arm_dry_run"):
            ns[name] = on_tab(name)
        ns["start_recording"] = self.start_recording
        ns["stop_recording"] = self.stop_recording
        ns["start_screencast"] = self.start_screencast
        ns["stop_screencast"] = self.stop_screencast
        # Retrieval deliberately returns the exact original. The invocation-wide stdout
        # ceiling still prevents printing it wholesale; callers can inspect or slice it
        # in Python without paying a second browser round trip.
        ns["fetch_content"] = self.content_store.get
        # Agent-written helpers load LAST and are executed with this namespace as their
        # globals, so an extension calls goto()/snapshot()/fill_form() exactly as a script
        # does — and its own functions are in scope for every script from the next run on.
        self.extensions = extend.load_into(ns)
        return ns

    def _bound_agent_value(self, surface: str, value: Any) -> Any:
        """Reversibly elide large JSON-like public results, never internal CDP traffic."""
        # Outcome objects carry behavior (`ok`, `unwrap`, typed failures) in addition to
        # data. Replacing one by a dict would destroy that contract; stdout remains capped
        # and their browser payloads are already bounded at source.
        if hasattr(value, "to_json") and not type(value) in (dict, list, str, bytes):
            return value
        # A reversible marker is a promise that the exact value exists. Storage or
        # serialization failure must surface instead of returning an unbounded value or
        # advertising a digest that cannot be fetched.
        shaped = self.content_store.elide(value, limit=self._value_limit,
                                           surface=surface)
        if shaped is value:
            return value
        with self._output_lock:
            self._output_elisions += 1
            self._output_spilled_bytes += int(shaped.get("_elided") or 0)
        self.journal.write(
            "note", event="output_elided", surface=surface,
            bytes=int(shaped.get("_elided") or 0), digest=shaped.get("_sha256"),
        )
        return shaped

    def output_stats(self) -> dict[str, int]:
        with self._output_lock:
            return {"helper_elisions": self._output_elisions,
                    "helper_spilled_bytes": self._output_spilled_bytes}


def force_utf8_streams() -> None:
    """Pin stdio to UTF-8 before an agent script runs. Idempotent.

    Upstream #359: on Windows, Python <3.15 defaults `sys.stdout` to the ANSI code page, so
    the plain `print(page_text())` that SKILL.md shows on nearly every example raises
    `UnicodeEncodeError` the moment a page contains CJK or an emoji — and it surfaces as a
    raw traceback through `run_script`'s `except BaseException: raise`, which is precisely
    the failure shape D11 exists to abolish. v1 fixed this in run.py; v2 had not.

    `stdin` is reconfigured for the same reason and it matters more: the *script* is read
    from it, so an umlaut in a form value could fail to decode before a single line ran.
    `errors="replace"` because a mangled character in a page dump is a far better outcome
    than losing the run — the harness never promised the page's bytes, only its text.
    """
    import sys
    # stdin gets utf-8-sig, the others plain utf-8: PowerShell's `>` and `Out-File` write a
    # UTF-8 BOM, and `bh < script.py` would otherwise hand the compiler a leading U+FEFF and
    # die on line 1 with an unhelpful SyntaxError. utf-8-sig strips it when present and is
    # identical to utf-8 when it is not; on the write side a BOM is never wanted.
    for stream, encoding in ((sys.stdin, "utf-8-sig"),
                             (sys.stdout, "utf-8"),
                             (sys.stderr, "utf-8")):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue                     # pytest capture, a plain StringIO, a closed stream
        try:
            reconfigure(encoding=encoding, errors="replace")
        except (ValueError, OSError):
            continue                     # already-read stdin, or a stream that refuses


def run_script(source: str, *, name: str = "default", filename: str = "<bh>") -> int:
    """Execute an agent-written script in a live session.

    A typed harness error is reported as its class and evidence rather than a traceback —
    the outcome contract is only useful if it survives to the surface the agent reads.
    """
    import json
    import sys
    import time

    force_utf8_streams()
    real_stdout = sys.stdout
    content_store = ContentStore()
    try:
        output_limit = max(0, int(os.environ.get(
            "BH_OUTPUT_BYTES", str(DEFAULT_OUTPUT_BYTES))))
    except ValueError:
        output_limit = DEFAULT_OUTPUT_BYTES
    captured = OutputCapture(real_stdout, content_store, limit=output_limit)
    sys.stdout = captured
    t0 = time.perf_counter()
    connected = exec_start = None
    session = None
    outcome: dict[str, Any] = {"ok": True}
    try:
        # Connecting is inside the try on purpose: "cannot reach the browser" is the most
        # likely failure of all, and it must reach the agent as a class with evidence, not
        # as a Python traceback from three frames deep in the client.
        session = Session(name, content_store=content_store)
        connected = time.perf_counter()
        ns = session.namespace()
        ns["__name__"] = "__bh__"
        exec_start = time.perf_counter()
        # bh's own argv must not leak into the script. Found the hard way: a live check
        # read `sys.argv[1]` and got `"-"` — bh's stdin flag — then asked the daemon to
        # attach to a target named "-". The daemon answered `target_gone` correctly, which
        # is the contract working, but the script should never have seen that argv at all.
        argv, sys.argv = sys.argv, [filename]
        try:
            exec(compile(source, filename, "exec"), ns)      # noqa: S102 — that is the product
        finally:
            sys.argv = argv
    except HarnessError as e:
        outcome = e.outcome.to_json()
        print(json.dumps(outcome, indent=2, default=str), file=sys.stderr)
        return 1
    except BaseException as e:
        outcome = {"ok": False, "class": type(e).__name__, "detail": str(e)[:200]}
        raise
    finally:
        sys.stdout = real_stdout
        output_stats = captured.emit()
        if session is not None:
            # ONE `bh` run == ONE model decision, and that is the number worth minimising:
            # a step costs seconds of model thinking against milliseconds of harness. The
            # journal documented an `invoke` kind from the start and never wrote one, so
            # step count — the dominant term — was the one thing it could not report.
            end = time.perf_counter()
            session.journal.write(
                "invoke", ok=outcome.get("ok", True),
                ms_total=round((end - t0) * 1000, 1),
                ms_connect=round(((connected or t0) - t0) * 1000, 1),
                ms_exec=round((end - (exec_start or end)) * 1000, 1),
                source_lines=source.count("\n") + 1,
                outcome=outcome, **session.output_stats(), **output_stats)
            session.close()
    return 0


__all__ = ["Session", "TargetGone", "force_utf8_streams", "run_script"]
