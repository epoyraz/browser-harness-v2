"""Agent-facing page primitives (DESIGN.md D3/D4/D11/D13/D14, TODO 15–21).

One `Tab` per target, built on the Phase 1 registry — a Tab never makes its own session.
The contract carried through every method:

  - Timeouts are **arguments, never env vars** (§6). v1's one global `BH_IPC_TIMEOUT`
    had to cover both a 5 ms box-model read and a 90 s in-page fetch.
  - Failures are typed with their evidence (D11): `goto()` returns `requested` AND
    `landed`, `js()` raises `JsException` with the description instead of returning None,
    a click returns a **delta** instead of silence.
  - Waits are event-driven (D13): `Page.lifecycleEvent` wakes a condition variable;
    nothing polls on a 300 ms loop.

The dialog dance, learned the hard way: `Input.dispatchMouseEvent` does not ACK while a
JS dialog opened by the click handler is up — the renderer is blocked inside our own
dispatch. So a click watches for `javascriptDialogOpening`, and a dispatch that times out
with a dialog pending is a *successful click that opened a dialog*, not a failure. The
dialog is auto-dismissed (accept=False by default) and reported in the delta.
"""
from __future__ import annotations

import base64
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from harness.connect.cdp import Connection
from harness.connect.session import SessionRegistry
from harness.core.journal import Journal
from harness.core.outcome import (
    Class,
    ElementGone,
    HarnessError,
    JsException,
    NavigationFailed,
    NotSerializable,
    Timeout,
)

#: The harness's machinery runs in a CDP **isolated world**, not on `window`.
#:
#: Measured: a page can read `Object.getOwnPropertyNames(window)` and see a stray
#: `__bh` global, which announces the harness for no benefit. An isolated world shares the
#: DOM but has its own global object, so page script cannot see our registry at all —
#: `Page.addScriptToEvaluateOnNewDocument(worldName=...)` recreates it on every navigation
#: for free. This is D14 ("use more of CDP, not more code"): the alternative was obfuscating
#: a global name, which only raises the cost of finding it.
#:
#: The user's own `js()` deliberately stays in the **main** world — it is the escape hatch,
#: and code that reaches for page globals must land where the page's globals live.
WORLD = "__bh_world"

#: Installed on every new document (item 18). Idempotent; `__bh.mutations` is the DOM
#: delta counter, `__bh.refs` the snapshot ref registry. Lives in the isolated world, so
#: `__bh` is reachable from harness JS and invisible to the page.
RUNTIME_JS = """(() => {
  if (window.__bh) return;
  const bh = window.__bh = {refs: {}, n: 0, mutations: 0};
  const obs = new MutationObserver(list => { bh.mutations += list.length; });
  const arm = () => obs.observe(document.documentElement || document,
    {subtree: true, childList: true, attributes: true, characterData: true});
  document.documentElement ? arm() : document.addEventListener('DOMContentLoaded', arm);
})()"""

#: One in-page pass over the interactive elements (item 20). Coordinates are viewport CSS
#: pixels from getBoundingClientRect — exactly what Input.dispatchMouseEvent takes.
SNAPSHOT_JS = """(() => {
  const bh = window.__bh || (window.__bh = {refs: {}, n: 0, mutations: 0});
  const sel = 'a[href],button,input,select,textarea,[role=button],[role=link],' +
    '[role=checkbox],[role=radio],[role=combobox],[role=menuitem],[role=tab],' +
    '[onclick],[contenteditable=true]';
  const out = [];
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    let ref = el.__bhRef;
    if (!ref || bh.refs[ref] !== el) { ref = 'e' + (++bh.n); el.__bhRef = ref; bh.refs[ref] = el; }
    const it = {ref, tag: el.tagName.toLowerCase(),
      name: (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder
             || el.name || '').trim().slice(0, 80),
      x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
      w: Math.round(r.width), h: Math.round(r.height)};
    if (el.disabled) it.disabled = true;
    if (el.type && el.type !== el.tagName.toLowerCase()) it.type = el.type;
    if (el.tagName === 'SELECT') it.options = el.options.length;
    out.push(it);
  }
  return out;
})()"""


def _unwrap_eval(r: dict[str, Any]) -> Any:
    """`Runtime.evaluate` result → a Python value, or the typed error. One implementation,
    shared by the main-world and isolated-world paths."""
    if ex := r.get("exceptionDetails"):
        desc = (ex.get("exception") or {}).get("description") or ex.get("text", "")
        raise JsException(desc.split("\n")[0][:300], line=ex.get("lineNumber"),
                          url=ex.get("url"), stack=desc[:1000])
    res = r.get("result") or {}
    if "value" in res or res.get("type") == "undefined":
        return res.get("value")
    # rule 3: a value we cannot hand over is an error, not a silent None (v1's bug)
    raise NotSerializable(
        f"result of type {res.get('subtype') or res.get('type')} has no JSON value",
        type=res.get("type"), description=(res.get("description") or "")[:120])


class _Waiter:
    """Buffers matching events from arming time, so nothing that fires between `navigate`
    returning and the wait starting can be missed."""

    __slots__ = ("cond", "hits", "pred")

    def __init__(self, pred):
        self.pred = pred
        self.cond = threading.Condition()
        self.hits: list[tuple[float, dict[str, Any]]] = []

    def offer(self, msg: dict[str, Any]) -> None:
        try:
            if not self.pred(msg):
                return
        except Exception:  # noqa: BLE001 — a bad predicate must not kill the reader
            return
        with self.cond:
            self.hits.append((time.perf_counter(), msg))
            self.cond.notify_all()

    def wait_match(self, pred, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                for _, m in self.hits:
                    if pred(m):
                        return m
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self.cond.wait(left)


class Tab:
    """Primitives bound to one target. All CDP goes through the target's registered
    session; all waits go through one subscriber registered at construction."""

    def __init__(self, conn: Connection, registry: SessionRegistry, target_id: str, *,
                 journal: Journal | None = None, accept_dialogs: bool = False):
        self._conn, self._reg, self.target_id = conn, registry, target_id
        self._j = journal or conn.journal
        self.accept_dialogs = accept_dialogs
        self._session_id: str | None = None
        self._wlock = threading.Lock()
        self._waiters: list[_Waiter] = []
        self._dialog: dict[str, Any] | None = None
        self._created: deque[dict[str, Any]] = deque(maxlen=16)
        self._world_ctx: int | None = None
        conn.subscribe(self._on_event)
        self._install_runtime()

    def close(self) -> None:
        self._conn.unsubscribe(self._on_event)

    @property
    def journal(self) -> Journal:
        return self._j

    # -- plumbing ----------------------------------------------------------

    def _sid(self) -> str:
        self._session_id = self._reg.ensure_live(self.target_id).session_id
        return self._session_id

    def _install_runtime(self) -> None:
        """Item 18: the registry + mutation counter exist on every document this tab will
        ever load, so refs survive navigation by reinstallation, not by luck — and they
        live in an isolated world, so the page never sees them."""
        sid = self._sid()
        self._conn.request(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": RUNTIME_JS, "worldName": WORLD, "runImmediately": True},
            session_id=sid, timeout=10.0)
        self._ensure_world()                                   # and for the current document

    def _ensure_world(self) -> int | None:
        """Isolated-world context id for the main frame, created on demand.

        Worlds die with their document, so this is re-resolved rather than cached across
        navigations; `executionContextsCleared` drops the stale id (see `_on_event`).
        """
        if self._world_ctx is not None:
            return self._world_ctx
        sid = self._sid()
        try:
            frame = self._conn.request("Page.getFrameTree", session_id=sid,
                                       timeout=10.0)["frameTree"]["frame"]["id"]
            ctx = self._conn.request(
                "Page.createIsolatedWorld",
                {"frameId": frame, "worldName": WORLD, "grantUniveralAccess": True},
                session_id=sid, timeout=10.0)["executionContextId"]
        except HarnessError:
            return None            # degrade to the main world rather than fail the call
        self._conn.request("Runtime.evaluate",
                           {"expression": RUNTIME_JS, "contextId": ctx},
                           session_id=sid, timeout=10.0)
        self._world_ctx = ctx
        return ctx

    def _world_js(self, expression: str, *, timeout: float = 10.0) -> Any:
        """Evaluate harness machinery in the isolated world. Falls back to the main world
        only if the world could not be created, so a degraded run still works."""
        ctx = self._ensure_world()
        if ctx is None:
            return self.js(expression, timeout=timeout)
        # replMode here too, for the same reason js() needs it: `fetch_all`'s template is a
        # top-level `await`, which is a syntax error without it (D14).
        params = {"expression": expression, "returnByValue": True, "awaitPromise": True,
                  "replMode": True, "contextId": ctx}
        try:
            r = self.cdp("Runtime.evaluate", params, timeout=timeout)
        except HarnessError as e:
            if e.cls is not Class.CDP_ERROR:
                raise
            self._world_ctx = None                 # context died under us; rebuild once
            ctx = self._ensure_world()
            if ctx is None:
                return self.js(expression, timeout=timeout)
            params["contextId"] = ctx
            r = self.cdp("Runtime.evaluate", params, timeout=timeout)
        return _unwrap_eval(r)

    def _on_event(self, msg: dict[str, Any]) -> None:
        """Reader thread: bookkeeping and waiter wakeups only, never a request."""
        sid = msg.get("sessionId")
        if sid is not None and sid != self._session_id:
            return                                     # another tab's event
        method = msg.get("method", "")
        params = msg.get("params") or {}
        if method in ("Runtime.executionContextsCleared", "Page.frameNavigated"):
            self._world_ctx = None            # the world died with its document
        if method == "Page.javascriptDialogOpening":
            with self._wlock:
                self._dialog = params
        elif method == "Target.targetCreated":
            info = params.get("targetInfo") or {}
            if info.get("type") == "page" and info.get("targetId") != self.target_id:
                self._created.append(info)
        with self._wlock:
            waiters = list(self._waiters)
        for w in waiters:
            w.offer(msg)

    @contextmanager
    def _armed(self, pred):
        w = _Waiter(pred)
        with self._wlock:
            self._waiters.append(w)
        try:
            yield w
        finally:
            with self._wlock:
                self._waiters.remove(w)

    # -- item 15: the escape hatches --------------------------------------

    def cdp(self, method: str, params: dict[str, Any] | None = None, *,
            timeout: float = 20.0) -> dict[str, Any]:
        return self._conn.request(method, params, session_id=self._sid(), timeout=timeout)

    def js(self, expression: str, *, timeout: float = 10.0,
           await_promise: bool = True) -> Any:
        """Evaluate with `replMode`, so top-level `await` and re-declared `const` work
        (D14) — v1 grew a wrap-and-retry heuristic instead and mis-wrapped nested returns.

        Sharp edge, measured: under replMode a **bare async IIFE** `(async()=>{...})()`
        resolves to `{}` — awaitPromise is effectively ignored there. Write top-level
        `await (async()=>{...})()` instead; replMode handles the await natively.
        """
        with self._j.call("js", expression=expression[:200]):
            r = self.cdp("Runtime.evaluate", {
                "expression": expression, "replMode": True, "returnByValue": True,
                "awaitPromise": await_promise}, timeout=timeout)
        return _unwrap_eval(r)

    # -- item 16 + 19: navigation and event-driven waits -------------------

    def goto(self, url: str, *, timeout: float = 20.0, wait_until: str = "load") -> dict[str, Any]:
        """Returns `{requested, landed}` or raises `NavigationFailed` carrying both.
        A 404 error page cannot be reported as a title (v1 did exactly that)."""
        with self._j.call("goto", url=url), \
             self._armed(lambda m: m.get("method") == "Page.lifecycleEvent") as w:
            nav = self.cdp("Page.navigate", {"url": url}, timeout=timeout)
            if err := nav.get("errorText"):
                raise NavigationFailed(err, requested=url, landed=self._try_url())
            loader = nav.get("loaderId")
            hit = w.wait_match(
                lambda m: (p := m.get("params") or {}).get("name") == wait_until
                and (not loader or p.get("loaderId") == loader),
                timeout,
            )
            if hit is None:
                raise Timeout(f"no {wait_until!r} lifecycle event in {timeout}s",
                              requested=url, wait_until=wait_until)
        landed = self._try_url() or url
        if landed.startswith("chrome-error://"):
            raise NavigationFailed("landed on an error page", requested=url, landed=landed)
        return {"requested": url, "landed": landed}

    def wait_lifecycle(self, name: str = "networkIdle", *, timeout: float = 10.0) -> None:
        with self._armed(lambda m: m.get("method") == "Page.lifecycleEvent") as w:
            if w.wait_match(lambda m: (m.get("params") or {}).get("name") == name,
                            timeout) is None:
                raise Timeout(f"no {name!r} lifecycle event in {timeout}s", wait=name)

    def _try_url(self) -> str | None:
        try:
            return self.js("location.href", timeout=5.0)
        except HarnessError:
            return None

    # -- items 17 + 20: snapshot, refs, and clicks that report a delta ------

    def snapshot(self) -> list[dict[str, Any]]:
        """Interactive elements with viewport-CSS coordinates, one round trip (item 20)."""
        with self._j.call("snapshot"):
            return self._world_js(SNAPSHOT_JS) or []

    def click_ref(self, ref: str, *, settle: float = 0.15, timeout: float = 10.0) -> dict[str, Any]:
        pre = self._world_js(
            f"(() => {{const el = window.__bh && __bh.refs[{ref!r}]; if (!el) return null;"
            " el.scrollIntoView({block: 'center', inline: 'center'});"
            " const r = el.getBoundingClientRect();"
            " return [r.x + r.width/2, r.y + r.height/2, location.href, __bh.mutations];})()", timeout=timeout)
        if pre is None:
            raise ElementGone(f"no element registered for ref {ref!r}", ref=ref)
        x, y, url_before, mut_before = pre
        return self._click(x, y, url_before, int(mut_before), settle, timeout, ref=ref)

    def click_at(self, x: float, y: float, *, settle: float = 0.15,
                 timeout: float = 10.0) -> dict[str, Any]:
        """Coordinate click — the default modality: compositor-level events pass through
        iframes and shadow roots that no selector can reach."""
        before = self._world_js("[location.href, window.__bh ? __bh.mutations : 0]",
                                timeout=timeout)
        return self._click(x, y, before[0], int(before[1]), settle, timeout)

    def _click(self, x: float, y: float, url_before: str, mut_before: int,
               settle: float, timeout: float, ref: str | None = None) -> dict[str, Any]:
        interesting = ("Page.lifecycleEvent", "Page.frameNavigated",
                       "Page.javascriptDialogOpening", "Target.targetCreated")
        targets_before = len(self._created)
        with self._j.call("click", x=x, y=y, ref=ref) , \
             self._armed(lambda m: m.get("method") in interesting) as w:
            for kind in ("mousePressed", "mouseReleased"):
                try:
                    self.cdp("Input.dispatchMouseEvent",
                             {"type": kind, "x": x, "y": y, "button": "left",
                              "clickCount": 1}, timeout=min(timeout, 2.0))
                except Timeout:
                    with self._wlock:
                        blocked_by_dialog = self._dialog is not None
                    if not blocked_by_dialog:
                        raise          # a real hang, not the dialog dance
                    break              # the click landed; its handler opened a dialog
            w.wait_match(lambda m: True, settle)       # first consequence, or settle elapses

        dialog = None
        with self._wlock:
            pending, self._dialog = self._dialog, None
        if pending is not None:
            self.cdp("Page.handleJavaScriptDialog", {"accept": self.accept_dialogs},
                     timeout=timeout)
            dialog = {"type": pending.get("type"), "message": pending.get("message")}

        post: list[Any] | None = None
        try:
            post = self._world_js("[location.href, window.__bh ? __bh.mutations : 0]",
                                  timeout=timeout)
        except HarnessError:
            pass                                       # e.g. the click closed the tab
        url_after = post[0] if post else None
        navigated = url_after is not None and url_after != url_before
        return {
            "url_before": url_before,
            "url_after": url_after,
            "navigated": navigated,
            # a new document restarts the counter, so a cross-document delta would lie
            "dom_mutations": None if navigated or post is None
                             else max(0, int(post[1]) - mut_before),
            "new_targets": [t.get("targetId") for t in list(self._created)[targets_before:]],
            "dialog": dialog,
        }

    # -- item 21: screenshots ----------------------------------------------

    def capture_screenshot(self, path: str | Path | None = None, *, quality: int = 70,
                           max_dim: int | None = None, timeout: float = 20.0) -> dict[str, Any]:
        """JPEG by default (PNG when the path says so); output pixels == CSS viewport
        pixels on any display: `clip.scale = 1/devicePixelRatio` (item 21). `max_dim`
        lowers the scale further instead of resizing afterwards."""
        fmt = "png" if str(path or "").endswith(".png") else "jpeg"
        with self._j.call("screenshot", format=fmt):
            m = self.cdp("Page.getLayoutMetrics", timeout=timeout)
            css = m.get("cssLayoutViewport") or m["layoutViewport"]
            cw, ch = css["clientWidth"], css["clientHeight"]
            dpr = float(self.js("devicePixelRatio", timeout=5.0) or 1)
            scale = 1.0 / dpr
            if max_dim:
                scale = min(scale, max_dim / (max(cw, ch) * dpr))
            params: dict[str, Any] = {"format": fmt, "clip": {
                "x": css.get("pageX", 0), "y": css.get("pageY", 0),
                "width": cw, "height": ch, "scale": scale}}
            if fmt == "jpeg":
                params["quality"] = quality
            data = base64.b64decode(self.cdp("Page.captureScreenshot", params,
                                             timeout=timeout)["data"])
        if path:
            Path(path).write_bytes(data)
        return {"path": str(path) if path else None, "bytes": len(data), "format": fmt,
                "css_viewport": [cw, ch], "scale": scale}
