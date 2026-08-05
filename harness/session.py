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
from typing import Any, Self

from harness.connect.client import RemoteConnection, RemoteRegistry, ensure_daemon
from harness.core.journal import Journal
from harness.core.outcome import HarnessError, TargetGone
from harness.ops import batch, forms
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
        self._current: str | None = None

    # -- tabs --------------------------------------------------------------

    def targets(self) -> list[dict[str, Any]]:
        infos = self.conn.request("Target.getTargets")["targetInfos"]
        return [t for t in infos if t.get("type") == "page"]

    def drivable(self) -> list[dict[str, Any]]:
        return [t for t in self.targets() if str(t.get("url", "")).startswith(_DRIVABLE)]

    def tab(self, target_id: str | None = None) -> Tab:
        """The current tab, or a named one. Attaches to a real page if we have none yet —
        and creates one rather than seizing a `chrome://` internal, which is not a page a
        caller ever meant."""
        tid = target_id or self._current
        if tid is None:
            pages = self.drivable()
            tid = pages[0]["targetId"] if pages else self.conn.request(
                "Target.createTarget", {"url": "about:blank"})["targetId"]
        if tid not in self._tabs:
            self._tabs[tid] = Tab(self.conn, self.registry, tid, journal=self.journal,
                                  accept_dialogs=self.accept_dialogs)
        self._current = tid
        return self._tabs[tid]

    def new_tab(self, url: str = "about:blank") -> Tab:
        """Create, attach, and make current. Always `about:blank` first, then navigate:
        passing a url to `createTarget` races the attach, so the brief blank page reads as
        'complete' and a wait returns before the real navigation starts (v1's comment)."""
        tid = self.conn.request("Target.createTarget", {"url": "about:blank"})["targetId"]
        tab = self.tab(tid)
        if url and url != "about:blank":
            tab.goto(url)
        return tab

    def use_tab(self, target_id: str) -> Tab:
        return self.tab(target_id)

    def close_tab(self, target_id: str | None = None) -> None:
        tid = target_id or self._current
        if tid is None:
            return
        if (tab := self._tabs.pop(tid, None)) is not None:
            tab.close()
        self.registry.forget(tid)
        if self._current == tid:
            self._current = None
        try:
            self.conn.request("Target.closeTarget", {"targetId": tid})
        except HarnessError:
            pass                      # already gone is the outcome we wanted

    def close(self) -> None:
        for tab in self._tabs.values():
            tab.close()
        self._tabs.clear()
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
            "targets": self.targets, "journal": self.journal,
            "form_schema": with_tab(forms.form_schema),
            "fill_form": with_tab(forms.fill_form),
            "set_value": with_tab(forms.set_value),
            "require_form": forms.require_form,
            "fetch_all": with_tab(batch.fetch_all),
        }
        for name in ("goto", "js", "cdp", "snapshot", "see", "click_ref", "click_at",
                     "capture_screenshot", "wait_lifecycle", "page_text", "press_key",
                     "scroll", "upload_file"):
            ns[name] = on_tab(name)
        return ns


def run_script(source: str, *, name: str = "default", filename: str = "<bh>") -> int:
    """Execute an agent-written script in a live session.

    A typed harness error is reported as its class and evidence rather than a traceback —
    the outcome contract is only useful if it survives to the surface the agent reads.
    """
    import json
    import sys

    session = None
    try:
        # Connecting is inside the try on purpose: "cannot reach the browser" is the most
        # likely failure of all, and it must reach the agent as a class with evidence, not
        # as a Python traceback from three frames deep in the client.
        session = Session(name)
        ns = session.namespace()
        ns["__name__"] = "__bh__"
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
        print(json.dumps(e.outcome.to_json(), indent=2, default=str), file=sys.stderr)
        return 1
    finally:
        if session is not None:
            session.close()
    return 0


__all__ = ["Session", "TargetGone", "run_script"]
