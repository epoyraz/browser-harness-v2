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

import math
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
from harness.ops import batch, forms
from harness.ops import parallel as parallel_ops
from harness.ops.page import Tab
from harness.skills import Registry as SkillRegistry


def _enabled(value: str | None, *, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in ("", "0", "false", "no", "off")




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
        self._skill_registry: SkillRegistry | None = None
        #: Teardown owned by layers above the harness — see `at_close`.
        self._at_close: list[Callable[[], None]] = []
        self.extensions: list[dict[str, Any]] = []
        if lease := os.environ.get("BH_TARGET_LEASE"):
            # An explicit lease takes precedence over the ergonomic first-tab fallback.
            # If it cannot be claimed, fail closed instead of driving whatever happens to
            # be first in Chrome after this fresh process connects.
            self.resume_lease(lease)

    # -- tabs --------------------------------------------------------------

    def targets(self) -> list[dict[str, Any]]:
        infos = self.conn.request("Target.getTargets")["targetInfos"]
        return [t for t in infos if t.get("type") == "page"]

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
        the attach lands; the retry excludes it and asks again.

        A daemon too old to know `adopt` used to fall back to the client-side scan this
        exists to prevent — a silent downgrade to the collision. `adopt` shipped without a
        protocol bump, which is what made that path reachable at all; the bump is the fix,
        so such a daemon now fails the handshake with a typed `ProtocolMismatch` that says
        to restart it.
        """
        tid = target_id or self._current
        if tid is not None:
            return self._attach(tid)
        exclude: list[str] = []
        last: HarnessError | None = None
        for _ in range(4):
            adopted = self.conn.adopt_default_target(exclude=exclude)
            try:
                return self._attach(adopted["target_id"])
            except HarnessError as error:
                last = error
                exclude.append(adopted["target_id"])
        raise last if last is not None else TargetGone("no adoptable tab could be attached")

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

    def new_tab(self, url: str = "about:blank", *, context_id: str | None = None,
                new_window: bool = False) -> Tab:
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
        if new_window:
            # Its own window makes the tab the selected tab of that window, so pages that
            # only paint while visible render without `activate_tab()` — an experiment
            # switch (`parallel(own_window=True)`), measured 2026-08-29.
            params["newWindow"] = True
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

    def place_window(self, target_id: str, *, slot: int, slots: int) -> dict[str, Any]:
        """Tile a tab's own window into cell `slot` of a grid of `slots` on the screen.

        `Target.createTarget(newWindow, background)` stacks every worker window at the
        same cascade position behind whatever the user is looking at: the user sees
        nothing, and Windows stops painting occluded windows, which leaves SPAs that only
        render while visible (the Abacus jobportal, 2026-08-29) blank. A grid keeps every
        worker window visible and unoccluded by its siblings.
        """
        tab = self.tab(target_id)
        screen = tab.js("({w: screen.availWidth, h: screen.availHeight, "
                        "l: screen.availLeft || 0, t: screen.availTop || 0})") or {}
        width_all = int(screen.get("w") or 1920)
        height_all = int(screen.get("h") or 1080)
        cols = max(1, math.ceil(math.sqrt(max(1, slots))))
        rows = max(1, math.ceil(max(1, slots) / cols))
        width = max(480, width_all // cols)
        height = max(420, height_all // rows)
        left = int(screen.get("l") or 0) + (slot % cols) * width
        top = int(screen.get("t") or 0) + ((slot // cols) % rows) * height
        window_id = self.conn.request("Browser.getWindowForTarget", {"targetId": target_id})["windowId"]
        # Bounds only apply to a "normal" window; a maximized one ignores them.
        self.conn.request("Browser.setWindowBounds",
                          {"windowId": window_id, "bounds": {"windowState": "normal"}})
        bounds = {"left": left, "top": top, "width": width, "height": height}
        self.conn.request("Browser.setWindowBounds", {"windowId": window_id, "bounds": bounds})
        self.journal.write("note", event="window_placed", target_id=target_id, slot=slot,
                           slots=slots, **bounds)
        return bounds

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






    def at_close(self, callback: Callable[[], None]) -> None:
        """Run `callback` when this session closes.

        A layer above the harness can own a resource whose lifetime is the session's
        without the harness knowing what it is — recording, for one, which used to be
        stopped by name in `close()`. Failures are journalled and never prevent the rest
        of teardown, because a cleanup that can abort cleanup is worse than none.
        """
        self._at_close.append(callback)


    def close(self) -> None:
        for callback in reversed(self._at_close):
            try:
                callback()
            except Exception as error:                          # noqa: BLE001
                self.journal.write("note", event="at_close_failed",
                                   error=f"{type(error).__name__}: {str(error)[:120]}")
        self._at_close.clear()
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
            "open_pages": open_pages,

            "account_credential_status": account_credential_status,
            "ensure_account_credential": ensure_account_credential,
            # Bound to this session, so a script writes parallel(urls, fn) and the bare
            # helpers inside fn address that worker's own tab.
            "parallel": parallel,
            "summarise": parallel_ops.summarise,
            "CancelToken": parallel_ops.CancelToken,
        }
        for name in ("goto", "open_page", "read_page", "js", "cdp", "snapshot", "see",
                     "find", "extract", "form_values", "ax",
                     "click_ref", "click_at",
                     "click_auth_ref",
                     "capture_screenshot", "wait_lifecycle", "wait_for", "wait_for_form",
                     "start_diagnostics", "diagnostics",
                     "frames",
                     "page_text", "press_key", "type_chars", "scroll", "upload_file",
                     "arm_dry_run"):
            ns[name] = on_tab(name)
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
