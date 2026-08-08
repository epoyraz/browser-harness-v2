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

import os
import threading
import time
from typing import Any, Self

from harness import extend
from harness.connect.client import RemoteConnection, RemoteRegistry, ensure_daemon
from harness.core.journal import Journal
from harness.core.outcome import Class, HarnessError, ScopeRefused, TargetGone
from harness.ops import batch, forms, record
from harness.ops import parallel as parallel_ops
from harness.ops.page import Tab

#: A tab we may drive. `about:blank` counts; chrome:// internals and devtools do not.
_DRIVABLE = ("http://", "https://", "file://", "about:blank", "data:")


class Session:
    """One client's view of the browser: a daemon connection and a current tab."""

    def __init__(self, name: str = "default", *, journal_path: str | None = None,
                 accept_dialogs: bool = False):
        self.name = name
        ensure_daemon(name)
        self.journal = Journal(journal_path or os.environ.get("BH_JOURNAL") or None,
                               session=name)
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
        self.extensions: list[dict[str, Any]] = []
        if os.environ.get("BH_RECORD", "").strip().lower() not in ("", "0", "false", "no"):
            self.start_recording()

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
                          accept_dialogs=self.accept_dialogs)
                with self._tabs_lock:
                    self._tabs[tid] = tab
        self._current = tid
        return tab

    def tab(self, target_id: str | None = None) -> Tab:
        """The current tab, or a named one. Attaches to a real page if we have none yet —
        and creates one rather than seizing a `chrome://` internal, which is not a page a
        caller ever meant.

        The fallback tries each drivable page rather than trusting the first. `getTargets`
        happily lists a tab that is already closing, and `parallel()` closes a whole
        worker pool's tabs at once — so the window where the first listed page is dead is
        wide, and taking it on faith raised `target_gone` from a call that had asked for
        nothing in particular.
        """
        tid = target_id or self._current
        if tid is not None:
            return self._attach(tid)
        for page in self.drivable():
            try:
                return self._attach(page["targetId"])
            except HarnessError:
                continue          # closing, or gone between the listing and the attach
        return self._attach(self.conn.request(
            "Target.createTarget", {"url": "about:blank"})["targetId"])

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
        'complete' and a wait returns before the real navigation starts (v1's comment)."""
        params = {"url": "about:blank"}
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

    def close_tab(self, target_id: str | None = None, *, wait: bool = True) -> None:
        tid = target_id or self._current
        if tid is None:
            return
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
                self.journal.write("note", event="resource_cleanup_failed",
                                   resource_kind="tab", identifier=tid,
                                   error=str(error)[:200])
                raise
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
            substantial = len(fields) >= 8 or (len(fields) >= 4 and data.get("file_inputs"))
            return bool((schema.get("verdict") or {}).get("is_form") or substantial)

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
                    bool(((item[1].get("schema") or {}).get("verdict") or {}).get("is_form")),
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

    # -- recording ---------------------------------------------------------

    def start_recording(self, name: str | None = None, title: str | None = None) -> str:
        """One frame per state-changing action, written beside the journal that explains it.

        Turning it on *moves* the journal into the recording directory: the frames and the
        calls that produced them become one artifact instead of two to correlate.
        """
        if self._recorder is not None:
            return str(self._recorder.dir)
        self._recorder = record.start(lambda: self._current and self._tabs.get(self._current),
                                      self.journal, name=name, title=title)
        record.prune()
        return str(self._recorder.dir)

    def stop_recording(self) -> str | None:
        if self._recorder is None:
            return None
        directory = self._recorder.stop()
        self._recorder = None
        return str(directory)

    def close(self) -> None:
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
                return getattr(self.tab(), fn_name)(*a, **kw)
            call.__name__ = fn_name
            return call

        def with_tab(fn):
            def call(*a: Any, **kw: Any) -> Any:
                return fn(self.tab(), *a, **kw)
            call.__name__ = fn.__name__
            return call

        ns: dict[str, Any] = {
            "session": self, "tab": self.tab, "new_tab": self.new_tab,
            "use_tab": self.use_tab, "close_tab": self.close_tab,
            "new_context": self.new_context, "close_context": self.close_context,
            "targets": self.targets, "journal": self.journal,
            "form_schema": with_tab(forms.form_schema),
            "fill_form": with_tab(forms.fill_form),
            "set_value": with_tab(forms.set_value),
            "select_option": with_tab(forms.select_option),
            "require_form": forms.require_form,
            "fetch_all": with_tab(batch.fetch_all),
            "application_route_candidates": forms.application_route_candidates,
            "prepare_application": self.prepare_application,
            "follow_application": self.follow_application,
            # Bound to this session, so a script writes parallel(urls, fn) and the bare
            # helpers inside fn address that worker's own tab.
            "parallel": lambda items, fn, **kw: parallel_ops.parallel(self, items, fn, **kw),
            "summarise": parallel_ops.summarise,
            "CancelToken": parallel_ops.CancelToken,
        }
        for name in ("goto", "js", "cdp", "snapshot", "see", "click_ref", "click_at",
                     "capture_screenshot", "wait_lifecycle", "wait_for", "wait_for_form",
                     "wait_for_application_state",
                     "frames",
                     "page_text", "press_key", "scroll", "upload_file", "arm_dry_run"):
            ns[name] = on_tab(name)
        ns["start_recording"] = self.start_recording
        ns["stop_recording"] = self.stop_recording
        # Agent-written helpers load LAST and are executed with this namespace as their
        # globals, so an extension calls goto()/snapshot()/fill_form() exactly as a script
        # does — and its own functions are in scope for every script from the next run on.
        self.extensions = extend.load_into(ns)
        return ns


def run_script(source: str, *, name: str = "default", filename: str = "<bh>") -> int:
    """Execute an agent-written script in a live session.

    A typed harness error is reported as its class and evidence rather than a traceback —
    the outcome contract is only useful if it survives to the surface the agent reads.
    """
    import json
    import sys
    import time

    t0 = time.perf_counter()
    connected = exec_start = None
    session = None
    outcome: dict[str, Any] = {"ok": True}
    try:
        # Connecting is inside the try on purpose: "cannot reach the browser" is the most
        # likely failure of all, and it must reach the agent as a class with evidence, not
        # as a Python traceback from three frames deep in the client.
        session = Session(name)
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
                outcome=outcome)
            session.close()
    return 0


__all__ = ["Session", "TargetGone", "run_script"]
