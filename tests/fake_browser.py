"""An in-process CDP browser, good enough to test session logic against.

Not a mock of our own code — a stand-in for Chrome that implements the `Target.*` semantics
the registry depends on: flattened attach, per-session domain state, and the lifecycle
events v1 never subscribed to. Replies are queued from worker threads, so they arrive
out of order exactly as a real browser's do; a test that passes here has actually exercised
id-multiplexing rather than a fortunate ordering.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any


class FakeBrowser:
    """Transport-compatible: `send` / `recv(timeout)` / `close`."""

    def __init__(self, *targets: str, latency: float = 0.0):
        self.targets: dict[str, dict[str, Any]] = {
            t: {"targetId": t, "type": "page", "url": f"https://{t}.test/"} for t in targets
        }
        self.sessions: dict[str, str] = {}                       # sessionId → targetId
        self.contexts: set[str] = set()
        self.enabled: dict[str, list[str]] = defaultdict(list)   # sessionId → domains
        self.calls: list[dict[str, Any]] = []
        self.attach_count: dict[str, int] = defaultdict(int)
        self.latency = latency
        self.in_flight = 0
        self.max_in_flight = 0
        #: Optional: expression → value for Runtime.evaluate. Return {"__raw__": {...}} to
        #: substitute the full CDP result payload (for exceptionDetails etc.).
        self.eval_hook = None
        #: Paths seen by the last DOM.setFileInputFiles.
        self.uploaded: list[str] = []
        #: Methods that never get a reply — simulates a renderer blocked by a JS dialog.
        self.hang_methods: set[str] = set()
        #: When set, Page.navigate reports this errorText (e.g. "net::ERR_CONNECTION_REFUSED").
        self.navigate_error: str | None = None
        #: Lifecycle events Page.navigate emits, in order. Default is a healthy document;
        #: set it to ["DOMContentLoaded", "networkAlmostIdle"] for a page held open by one
        #: stalled subresource, which never emits `load`.
        self.lifecycle_names: list[str] = ["load"]
        #: worldName of every Page.createIsolatedWorld — proves the machinery is off-window.
        self.isolated_worlds: list[str] = []

        self._q: deque[dict[str, Any]] = deque()
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._closed = False
        self._n = 0
        self._target_n = 0
        self._context_n = 0
        self._workers: list[threading.Thread] = []

    # -- transport interface ----------------------------------------------

    def send(self, msg: dict[str, Any]) -> None:
        if self._closed:
            raise EOFError("fake browser closed")
        with self._lock:
            self.calls.append(msg)
        worker = threading.Thread(target=self._work, args=(msg,), daemon=True)
        worker.start()
        self._workers.append(worker)

    def recv(self, timeout: float | None = None) -> dict[str, Any]:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                if self._q:
                    return self._q.popleft()
                if self._closed:
                    raise EOFError("fake browser closed")
                self._ready.clear()
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("no frame")
            self._ready.wait(0.02)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._ready.set()

    # -- emission ----------------------------------------------------------

    def emit(self, method: str, params: dict[str, Any] | None = None,
             session_id: str | None = None) -> None:
        """Push an unsolicited event, as the browser does."""
        frame: dict[str, Any] = {"method": method, "params": params or {}}
        if session_id:
            frame["sessionId"] = session_id
        self._push(frame)

    def _push(self, frame: dict[str, Any]) -> None:
        with self._lock:
            self._q.append(frame)
            self._ready.set()

    # -- behaviour ---------------------------------------------------------

    def _work(self, msg: dict[str, Any]) -> None:
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.latency:
                time.sleep(self.latency)
            if msg.get("method") in self.hang_methods:
                return                                   # deliberately never answered
            self._push(self._respond(msg))
        finally:
            with self._lock:
                self.in_flight -= 1

    def _respond(self, msg: dict[str, Any]) -> dict[str, Any]:
        method, params = msg.get("method", ""), msg.get("params") or {}
        session_id, msg_id = msg.get("sessionId"), msg.get("id")

        def err(message: str, code: int = -32000) -> dict[str, Any]:
            return {"id": msg_id, "error": {"code": code, "message": message}}

        if session_id and session_id not in self.sessions:
            return err("Session with given id not found.")

        if method == "Target.attachToTarget":
            target = params.get("targetId")
            if target not in self.targets:
                return err(f"No target with given id found: {target}")
            with self._lock:
                self._n += 1
                sid = f"S{self._n}"
                self.sessions[sid] = target
                self.attach_count[target] += 1
            self.emit("Target.attachedToTarget",
                      {"sessionId": sid, "targetInfo": self.targets[target]})
            return {"id": msg_id, "result": {"sessionId": sid}}

        if method == "Target.createBrowserContext":
            with self._lock:
                self._context_n += 1
                context_id = f"C{self._context_n}"
                self.contexts.add(context_id)
            return {"id": msg_id, "result": {"browserContextId": context_id}}

        if method == "Target.disposeBrowserContext":
            context_id = params.get("browserContextId")
            if context_id not in self.contexts:
                return err(f"Failed to find context with id {context_id}")
            doomed = [target_id for target_id, info in self.targets.items()
                      if info.get("browserContextId") == context_id]
            with self._lock:
                self.contexts.remove(context_id)
            for target_id in doomed:
                self.destroy(target_id)
            return {"id": msg_id, "result": {}}

        if method == "Target.createTarget":
            context_id = params.get("browserContextId")
            if context_id is not None and context_id not in self.contexts:
                return err(f"Failed to find context with id {context_id}")
            with self._lock:
                self._target_n += 1
                target_id = f"T{self._target_n}"
                info = {"targetId": target_id, "type": "page",
                        "url": str(params.get("url") or "about:blank")}
                if context_id is not None:
                    info["browserContextId"] = context_id
                self.targets[target_id] = info
            self.emit("Target.targetCreated", {"targetInfo": info})
            return {"id": msg_id, "result": {"targetId": target_id}}

        if method == "Target.closeTarget":
            target_id = params.get("targetId")
            if target_id not in self.targets:
                return err(f"No target with given id found: {target_id}")
            self.destroy(target_id)
            return {"id": msg_id, "result": {"success": True}}

        if method == "Target.getTargets":
            return {"id": msg_id, "result": {"targetInfos": list(self.targets.values())}}

        if method == "Target.getTargetInfo":
            target_id = params.get("targetId")
            if target_id not in self.targets:
                return err(f"No target with given id found: {target_id}")
            return {"id": msg_id, "result": {"targetInfo": self.targets[target_id]}}

        if method in ("Target.setDiscoverTargets", "Target.activateTarget"):
            return {"id": msg_id, "result": {}}

        if method.endswith(".enable"):
            if session_id:
                with self._lock:
                    self.enabled[session_id].append(method.split(".")[0])
            return {"id": msg_id, "result": {}}

        if method == "Page.navigate":
            if self.navigate_error:
                return {"id": msg_id,
                        "result": {"loaderId": "L1", "errorText": self.navigate_error}}
            # `lifecycle_names` models the page's behaviour: the default is a healthy
            # document, but a page with one stalled subresource emits DOMContentLoaded and
            # networkAlmostIdle and never emits `load` at all. Both orders are real and
            # goto has to tell them apart, so the fake has to be able to produce both.
            for name in self.lifecycle_names:
                # the events race the navigate reply, exactly as in real Chrome
                self.emit("Page.lifecycleEvent",
                          {"name": name, "loaderId": "L1", "frameId": "F1"},
                          session_id=session_id)
            return {"id": msg_id, "result": {"loaderId": "L1", "frameId": "F1"}}

        if method == "Page.getFrameTree":
            return {"id": msg_id, "result": {"frameTree": {"frame": {"id": "F1"}}}}

        if method == "Page.createIsolatedWorld":
            # The harness's machinery runs here, so the page's `window` never sees it.
            self.isolated_worlds.append(params.get("worldName"))
            return {"id": msg_id, "result": {"executionContextId": 77}}

        if method == "Page.getLayoutMetrics":
            return {"id": msg_id, "result": {"cssLayoutViewport": {
                "pageX": 0, "pageY": 0, "clientWidth": 1200, "clientHeight": 800}}}

        if method == "Page.captureScreenshot":
            import base64
            return {"id": msg_id,
                    "result": {"data": base64.b64encode(b"fake-image-bytes").decode()}}

        if method == "DOM.describeNode":
            # upload_file bridges a JS handle to a backendNodeId through here.
            return {"id": msg_id, "result": {"node": {"backendNodeId": 4242}}}

        if method == "DOM.setFileInputFiles":
            self.uploaded = list(params.get("files") or [])
            return {"id": msg_id, "result": {}}

        if method == "Runtime.evaluate":
            if self.eval_hook is not None:
                value = self.eval_hook(params.get("expression", ""))
                if isinstance(value, dict) and "__raw__" in value:
                    return {"id": msg_id, "result": value["__raw__"]}
                return {"id": msg_id, "result": {"result": {"type": "object", "value": value}}}
            # Echo both the target and the expression, so a test can prove which tab a call
            # landed on *and* that a reply was matched to the request that asked for it.
            target = self.sessions.get(session_id or "", "<browser>")
            return {"id": msg_id, "result": {"result": {
                "type": "string", "value": target, "echo": params.get("expression")}}}

        return {"id": msg_id, "result": {}}

    # -- test affordances --------------------------------------------------

    def destroy(self, target_id: str) -> None:
        """Close a tab the way Chrome does: invalidate its sessions, then announce it."""
        with self._lock:
            self.targets.pop(target_id, None)
            dead = [s for s, t in self.sessions.items() if t == target_id]
            for s in dead:
                self.sessions.pop(s, None)
        for s in dead:
            self.emit("Target.detachedFromTarget", {"sessionId": s, "targetId": target_id})
        self.emit("Target.targetDestroyed", {"targetId": target_id})

    def crash(self, target_id: str) -> None:
        self.emit("Target.targetCrashed", {"targetId": target_id, "reason": "oom"})

    def domains_for(self, target_id: str) -> list[str]:
        with self._lock:
            for sid, tid in self.sessions.items():
                if tid == target_id:
                    return list(self.enabled[sid])
        return []
