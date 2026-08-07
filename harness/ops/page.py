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
import json
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

#: Extension -> MIME for reading a file input's `accept`. Only the types upload controls
#: realistically advertise; anything else is treated as admissible, because the job here
#: is to name a definite client-side rejection, never to invent one.
_MIME = {".pdf": "application/pdf", ".doc": "application/msword",
         ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         ".txt": "text/plain", ".rtf": "application/rtf",
         ".odt": "application/vnd.oasis.opendocument.text",
         ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".gif": "image/gif", ".webp": "image/webp"}


def _resolve_js(ref: str) -> str:
    """JS that resolves `ref` as a snapshot ref, falling back to a CSS selector.

    Refs come from `snapshot()`, which only registers elements that have a box. A file
    input almost never does: the standard pattern is a `display:none` input behind a
    styled dropzone, so `upload_file` was unreachable for exactly the case it exists to
    serve — every ATS, and joblens' own CV field. Accepting a selector costs one `||` and
    removes the need to drop to raw CDP.

    `querySelector` throws on a non-selector string (a bare ref id like `e12`), so the
    fallback is guarded — an unregistered ref must read as "not found", not as an error.
    """
    return (f"((window.__bh && window.__bh.refs && window.__bh.refs[{ref!r}])"
            f" || (() => {{ try {{ return document.querySelector({ref!r}); }}"
            f" catch (e) {{ return null; }} }})())")


def _accepts(accept: str, path: str) -> bool:
    """Would this input's `accept` admit this file? An empty filter or an unknown
    extension admits — a false rejection would be worse than no check at all."""
    if not accept.strip():
        return True
    ext = Path(path).suffix.lower()
    mime = _MIME.get(ext)
    for tok in (t.strip().lower() for t in accept.split(",")):
        if not tok:
            continue
        if tok.startswith("."):
            if tok == ext:
                return True
        elif tok.endswith("/*"):
            if mime and mime.startswith(tok[:-1]):
                return True
        elif mime and tok == mime:
            return True
    return mime is None


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
    const cs = getComputedStyle(el);
    const invisible = !r.width || !r.height
      || cs.visibility === 'hidden' || cs.display === 'none';
    // A file input is never clicked — clicking opens a native picker that blocks the
    // renderer with no CDP way back out — so upload_file() always drives it
    // programmatically. Visibility therefore says nothing about whether it is reachable,
    // and every dropzone UI hides the real input behind a styled div. Excluding it left
    // the ordinary ATS upload with no ref at all, so upload_file() silently took the
    // nearest visible file input instead.
    const isFile = el.tagName === 'INPUT' && el.type === 'file';
    if (invisible && !isFile) continue;
    let ref = el.__bhRef;
    if (!ref || bh.refs[ref] !== el) { ref = 'e' + (++bh.n); el.__bhRef = ref; bh.refs[ref] = el; }
    const it = {ref, tag: el.tagName.toLowerCase(),
      name: (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder
             || el.name || '').trim().slice(0, 80),
      x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2),
      w: Math.round(r.width), h: Math.round(r.height)};
    if (el.disabled) it.disabled = true;
    if (el.type && el.type !== el.tagName.toLowerCase()) it.type = el.type;
    if (invisible) it.hidden_control = true;
    if (el.tagName === 'SELECT') it.options = el.options.length;
    out.push(it);
  }
  return out;
})()"""


#: Draw a labelled box over every snapshot ref, so a screenshot and the structured
#: element list share one index (set-of-mark). Injected from the isolated world, but the
#: nodes must live in the page's own DOM or the renderer would not paint them into the
#: capture; they are removed immediately afterwards.
#:
#: Why this exists: structured extraction and vision each fail where the other is strong.
#: A schema cannot see that a control is a 1x1 clipped decoy — it read back byte-identical
#: and submitted nothing. A screenshot cannot see 249 collapsed <option>s, and a model
#: reading coordinates off an image estimates them. Sharing an index removes the trade:
#: look at the picture, act on the ref.
ANNOTATE_JS = """((els) => {
  const prev = document.getElementById('__bh_marks');
  if (prev) prev.remove();
  const layer = document.createElement('div');
  layer.id = '__bh_marks';
  layer.style.cssText =
    'position:fixed;inset:0;z-index:2147483647;pointer-events:none;font:11px/1.2 ' +
    'ui-monospace,Menlo,monospace';
  for (const e of els) {
    if (!e.w || !e.h) continue;                       // hidden controls have no box to draw
    const b = document.createElement('div');
    b.style.cssText =
      `position:absolute;left:${e.x - e.w / 2}px;top:${e.y - e.h / 2}px;` +
      `width:${e.w}px;height:${e.h}px;outline:2px solid #e0115f;` +
      'outline-offset:-1px;background:rgba(224,17,95,.06)';
    const tag = document.createElement('span');
    tag.textContent = e.ref;
    tag.style.cssText =
      'position:absolute;left:0;top:-14px;padding:0 3px;background:#e0115f;' +
      'color:#fff;border-radius:2px;white-space:nowrap';
    b.appendChild(tag);
    layer.appendChild(b);
  }
  document.documentElement.appendChild(layer);
  return els.length;
})(__ELS__)"""


#: Name of the isolated-world binding a watcher calls when its condition becomes true.
#: Scoped to WORLD via `executionContextName`, NOT global: an unscoped `Runtime.addBinding`
#: puts a function on the page's own `window`, which is the exact detectability leak the
#: isolated world was introduced to close.
BINDING = "__bhNotify"

#: Wait for a selector without polling (D13). Evaluates once, and only if that misses does
#: it arm a MutationObserver that re-checks and fires the binding. My own live checks are
#: littered with `time.sleep(1.0)` because this did not exist — a guessed sleep is both
#: slower than it needs to be and wrong when the page is slower than the guess.
WATCH_JS = """((sel, state, token) => {
  const ok = () => {
    const e = document.querySelector(sel);
    if (state === 'gone') return !e;
    if (!e) return false;
    if (state === 'visible') {
      const r = e.getBoundingClientRect();
      const cs = getComputedStyle(e);
      return !!(r.width && r.height) && cs.visibility !== 'hidden' && cs.display !== 'none';
    }
    return true;
  };
  if (ok()) return {matched: true, immediate: true};
  const bh = window.__bh || (window.__bh = {refs: {}, n: 0, mutations: 0});
  bh.watch = bh.watch || {};
  const obs = new MutationObserver(() => {
    if (!ok()) return;
    obs.disconnect();
    delete bh.watch[token];
    __bhNotify(token);
  });
  obs.observe(document.documentElement || document,
    {subtree: true, childList: true, attributes: true, characterData: true});
  bh.watch[token] = obs;
  return {matched: false, immediate: false};
})(__SEL__, __STATE__, __TOKEN__)"""


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


#: keyName -> (code, text). Only printable keys carry text; see press_key.
_KEYS = {
    "Enter": ("Enter", "\r"), "Tab": ("Tab", "\t"), "Backspace": ("Backspace", ""),
    "Escape": ("Escape", ""), "Delete": ("Delete", ""), " ": ("Space", " "),
    "ArrowLeft": ("ArrowLeft", ""), "ArrowRight": ("ArrowRight", ""),
    "ArrowUp": ("ArrowUp", ""), "ArrowDown": ("ArrowDown", ""),
    "Home": ("Home", ""), "End": ("End", ""), "PageUp": ("PageUp", ""),
    "PageDown": ("PageDown", ""),
}


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
        self._bound = False
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
        if not self._bound:
            try:
                # executionContextName scopes the binding to the isolated world, so the
                # page's own `window` never gains a `__bhNotify` to detect.
                self._conn.request("Runtime.addBinding",
                                   {"name": BINDING, "executionContextName": WORLD},
                                   session_id=sid, timeout=10.0)
                self._bound = True
            except HarnessError:
                pass                  # waits fall back to their timeout, nothing else breaks
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

    # -- waiting on a condition, not on a guess ----------------------------

    def wait_for(self, selector: str, *, state: str = "visible", timeout: float = 10.0,
                 settle: float = 0.0) -> dict[str, Any]:
        """Wait for `selector` to be present / visible / gone. Event-driven, never polled.

        The reliability primitive v2 was missing. `wait_lifecycle` answers "the document
        loaded", which an SPA satisfies long before the thing you need exists — so every
        script (mine included: 16 of them across the live checks) fell back to
        `time.sleep(1.2)`, which is simultaneously too slow when the page is fast and wrong
        when the page is slow.

        `state` is `visible` by default rather than `present`: a node that exists but has
        no box is the failure mode that produced a verified write to a 1x1 decoy.
        """
        if state not in ("present", "visible", "gone"):
            raise ValueError(f"state must be present|visible|gone, got {state!r}")
        token = f"w{id(self)}:{time.perf_counter_ns()}"
        with self._j.call("wait_for", selector=selector, state=state):
            # Arm the Python-side waiter BEFORE evaluating: a fast page can satisfy the
            # condition and fire the binding between the evaluate and the wait.
            with self._armed(lambda m: m.get("method") == "Runtime.bindingCalled") as w:
                probe = self._world_js(
                    WATCH_JS.replace("__SEL__", json.dumps(selector))
                            .replace("__STATE__", json.dumps(state))
                            .replace("__TOKEN__", json.dumps(token)),
                    timeout=timeout) or {}
                if not probe.get("matched"):
                    hit = w.wait_match(
                        lambda m: (m.get("params") or {}).get("payload") == token, timeout)
                    if hit is None:
                        self._unwatch(token)
                        raise Timeout(
                            f"{selector!r} was not {state} within {timeout}s",
                            selector=selector, state=state, timeout=timeout)
            if settle:
                time.sleep(settle)
        return {"selector": selector, "state": state,
                "immediate": bool(probe.get("immediate"))}

    def _unwatch(self, token: str) -> None:
        """Drop an abandoned observer. A MutationObserver left armed on a busy page runs
        its callback on every DOM change for the life of the document."""
        try:
            self._world_js(
                f"(() => {{const w = window.__bh && __bh.watch;"
                f" if (w && w[{json.dumps(token)}]) {{w[{json.dumps(token)}].disconnect();"
                f" delete w[{json.dumps(token)}];}} return true;}})()", timeout=5.0)
        except HarnessError:
            pass

    def frames(self) -> list[dict[str, Any]]:
        """Cross-origin iframes as attachable targets.

        Same-origin iframes are reachable from `js()` through `contentDocument`; a
        cross-origin one is a separate CDP target and is invisible to every DOM call on the
        parent. Measured live: a SmartRecruiters posting behind DataDome had
        `body.innerText.length === 0` and 10 nodes, with the entire real page inside a
        `geo.captcha-delivery.com` iframe. Without this the page reads as broken rather
        than as bot-walled.

        Attach with `session.tab(target_id)`.
        """
        # Auto-attach is the ONLY way an OOPIF becomes reachable: `Target.getTargets`
        # never lists one (measured: types are page/tab/service_worker/background_page
        # only, even with an explicit filter and --site-per-process), and
        # `attachToTarget` rejects its frame id. Turning it on makes the browser announce
        # each child via `Target.attachedToTarget`, which the registry books.
        def announce(retoggle: bool) -> list[dict[str, Any]]:
            got: list[dict[str, Any]] = []
            with self._armed(lambda m: m.get("method") == "Target.attachedToTarget") as w:
                if retoggle:
                    # Enabling auto-attach when it is ALREADY on is a no-op, so a second
                    # call in the same daemon-backed session announces nothing and the
                    # page reads as frameless. Toggling forces a full re-announcement.
                    self.cdp("Target.setAutoAttach",
                             {"autoAttach": False, "waitForDebuggerOnStart": False,
                              "flatten": True}, timeout=10.0)
                self.cdp("Target.setAutoAttach",
                         {"autoAttach": True, "waitForDebuggerOnStart": False,
                          "flatten": True}, timeout=10.0)
                w.wait_match(lambda m: True, 0.6)      # let the announcements arrive
                for _, msg in w.hits:
                    info = (msg.get("params") or {}).get("targetInfo") or {}
                    if info.get("type") == "iframe":
                        got.append({"target_id": info["targetId"],
                                    "url": info.get("url", ""), "kind": "oopif",
                                    "reachable": "session.tab(target_id)"})
            return got

        out = announce(False) or announce(True)
        # Same-site iframes stay in the parent process and never become targets, so
        # getTargets alone reads as "no iframes" on a page that plainly has one.
        try:
            same = self._world_js(
                "[...document.querySelectorAll('iframe')].map(f => ({src: f.src || '',"
                " same: (() => {try { return !!f.contentDocument; } catch (e) "
                "{ return false; }})()}))", timeout=10.0) or []
        except HarnessError:
            same = []
        for f in same:
            if f.get("same"):
                out.append({"target_id": None, "url": f.get("src", ""),
                            "kind": "same-document", "reachable": "js/contentDocument"})
        return out

    # -- vision: the other half of perception ------------------------------

    def see(self, path: str | Path | None = None, *, marks: bool = True,
            max_dim: int | None = 1400, quality: int = 70,
            timeout: float = 20.0) -> dict[str, Any]:
        """One perception act: the structured elements **and** a screenshot they index.

        v1 ships no extraction helper at all — its SKILL.md hands the agent the
        `getFullAXTree` + `getBoxModel` recipe and says "screenshot when layout or imagery
        matters", so perception is whatever the model writes or sees. v2 shipped one fixed
        extractor instead, which is faster and cheaper (a form schema is ~175 tokens where
        its screenshot is ~3,200) but has exactly one way to be blind, and was: a Select2
        decoy that a schema read as a real field and a human eye would never have typed
        into.

        So neither channel is the default. `see()` returns both, sharing one index: every
        box drawn on the image carries its `ref`, so looking at the picture and acting on
        the DOM are the same decision. `marks=False` gives a clean frame for a human.

        The returned `elements` are the same objects `snapshot()` returns.
        """
        with self._j.call("see", marks=marks):
            els = self._world_js(SNAPSHOT_JS, timeout=timeout) or []
            drawn = 0
            if marks and els:
                drawn = self._world_js(
                    ANNOTATE_JS.replace("__ELS__", json.dumps(els)), timeout=timeout) or 0
            try:
                shot = self.capture_screenshot(path, max_dim=max_dim, quality=quality,
                                               timeout=timeout)
            finally:
                if marks and els:
                    # Always clear the overlay, even if the capture failed — leaving it
                    # would change what every later click lands on.
                    self._world_js(
                        "(() => {const m = document.getElementById('__bh_marks');"
                        " if (m) m.remove(); return true;})()", timeout=timeout)
        return {**shot, "elements": els, "marked": drawn}

    # -- the rest of the promised surface ----------------------------------

    def page_text(self, max_chars: int = 40_000) -> str:
        """Rendered text, truncated. `innerText` not `textContent`: the latter includes
        script bodies and hidden nodes, which is how a "page text" read becomes 200 KB of
        minified JS."""
        with self._j.call("page_text"):
            return self._world_js(
                f"(document.body ? document.body.innerText : '').slice(0, {max_chars})",
                timeout=15.0) or ""

    def press_key(self, key: str, *, modifiers: int = 0, timeout: float = 10.0) -> None:
        """One named key. `text` is sent only for printable keys — attaching it to Enter or
        Tab makes Chrome insert a character instead of firing the shortcut (v1 paid for
        this with an uncleared field)."""
        spec = _KEYS.get(key)
        code, text = (spec if spec else (key, key if len(key) == 1 else ""))
        base: dict[str, Any] = {"key": key, "code": code, "modifiers": modifiers}
        down = {**base, "type": "keyDown"}
        if text and not modifiers:
            down["text"] = text
        with self._j.call("press_key", key=key):
            self.cdp("Input.dispatchKeyEvent", down, timeout=timeout)
            self.cdp("Input.dispatchKeyEvent", {**base, "type": "keyUp"}, timeout=timeout)

    def scroll(self, dy: int = 600, dx: int = 0, *, x: int = 400, y: int = 300,
               timeout: float = 10.0) -> dict[str, Any]:
        """Wheel event at a point, so it scrolls whatever container is under the cursor —
        an overflow pane, a virtualised list — not just the document."""
        with self._j.call("scroll", dy=dy, dx=dx):
            self.cdp("Input.dispatchMouseEvent",
                     {"type": "mouseWheel", "x": x, "y": y, "deltaX": dx, "deltaY": dy},
                     timeout=timeout)
        return self._world_js(
            "({y: Math.round(scrollY), height: document.documentElement.scrollHeight,"
            " atBottom: Math.ceil(scrollY + innerHeight) >= document.documentElement.scrollHeight})",
            timeout=timeout)

    def upload_file(self, ref: str, paths: str | list[str], *,
                    timeout: float = 20.0) -> dict[str, Any]:
        """Set a file input's files without touching the OS picker.

        `ref` is a snapshot ref **or** a CSS selector — hidden file inputs never reach the
        snapshot registry, so a selector is usually the only way to name one.

        `DOM.setFileInputFiles` needs a backendNodeId, and the ref registry holds a JS
        handle — so the bridge is `Runtime.evaluate(returnByValue=false)` to get an object
        id, then `DOM.describeNode`. Clicking the input instead would open a native dialog
        that blocks the renderer with no CDP way back out.

        The return says what actually happened, because `attached: []` alone cannot
        distinguish three different outcomes: a page whose change handler consumed the
        file and cleared the input (success), a file the input's `accept` filtered out,
        and a ref that was never a file input. The second and third are now loud —
        pointing this at the wrong element used to look exactly like success.
        """
        files = [paths] if isinstance(paths, str) else list(paths)
        missing = [f for f in files if not Path(f).is_file()]
        if missing:
            raise ElementGone(f"no such file(s): {missing}", files=missing)
        resolve = _resolve_js(ref)
        ctx = self._ensure_world()
        params: dict[str, Any] = {"expression": resolve, "returnByValue": False}
        if ctx is not None:
            params["contextId"] = ctx
        handle = self.cdp("Runtime.evaluate", params, timeout=timeout).get("result") or {}
        if not handle.get("objectId"):
            raise ElementGone(
                f"no element for {ref!r} — not a registered ref, and no element matches it "
                f"as a CSS selector", ref=ref)
        el = self._world_js(
            f"(() => {{const e = {resolve}; if (!e) return null;"
            " return {tag: e.tagName.toLowerCase(), type: e.type || null,"
            "  name: e.name || e.id || null, accept: e.accept || ''};})()",
            timeout=timeout) or {}
        if el.get("tag") != "input" or el.get("type") != "file":
            # The failure that motivated this check: snapshot() skipped a display:none
            # CV input, so the only file ref on the page was an unrelated 1x1 control,
            # and setting files on it reported `attached: []` — indistinguishable from
            # the success case. Refuse instead of guessing.
            raise ElementGone(
                f"ref {ref!r} is <{el.get('tag')} type={el.get('type')!r}>, not a file "
                f"input — setting files on it would silently do nothing",
                ref=ref, tag=el.get("tag"), type=el.get("type"))
        node = self.cdp("DOM.describeNode", {"objectId": handle["objectId"]},
                        timeout=timeout)["node"]
        with self._j.call("upload_file", ref=ref, n=len(files)):
            self.cdp("DOM.setFileInputFiles",
                     {"files": [str(Path(f).resolve()) for f in files],
                      "backendNodeId": node["backendNodeId"]}, timeout=timeout)
        got = self._world_js(
            f"(() => {{const e = {resolve}; if (!e) return [];"
            f" return [...(e.files||[])].map(f => f.name);}})()", timeout=timeout)
        out: dict[str, Any] = {"ref": ref, "attached": got or [], "requested": len(files),
                               "accept": el.get("accept") or ""}
        if not got:
            # Empty is normal when the page's change handler moves the file into its own
            # state and clears the input. It is NOT normal when `accept` excluded the file
            # — that one is a silent client-side rejection, so name it.
            rejected = [f for f in files if not _accepts(el.get("accept") or "", f)]
            out["consumed_or_rejected"] = True
            if rejected:
                out["accept_rejected"] = [Path(f).name for f in rejected]
        return out

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
