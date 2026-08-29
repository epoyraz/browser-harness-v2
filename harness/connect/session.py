"""Session registry: `{targetId → sessionId}` (DESIGN.md D1/D11a, TODO 8–10).

This is the part v1 got wrong four separate times, and the reason eleven session/tab PRs
were opened against it. v1 had **three** places that produced a session:

  - `daemon.attach_first_page()`  — attached, then enabled Page/DOM/Runtime/Network
  - `helpers.switch_tab()`        — attached, told the daemon, enabled nothing itself
  - `helpers.js()`                — attached a *fresh session per call* for iframe targets,
                                    enabled nothing, detached never

Three producers, one of which enabled domains. So `wait_for_network_idle()` worked on the
tab you started on and silently received no events on any tab you switched to — v1's own
comment says exactly this. Here there is **exactly one producer**, `ready_session()`, and a
test asserts no other path can enable a domain.

The second half is knowing when a session *died*. v1 contains no `setDiscoverTargets`, no
`setAutoAttach`, and handles no `detachedFromTarget`, `targetDestroyed` or `targetCrashed`
event — so it could only discover a dead session by issuing a command, failing, and
string-matching Chrome's prose out of the error. v2 subscribes to target lifecycle and
marks the session dead when the browser says so, which turns `ensure_live()` from a probe
round trip into a dictionary lookup.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from harness.connect.cdp import Connection
from harness.core.journal import Journal
from harness.core.outcome import Class, HarnessError, Outcome

#: Enabled on every session, in one place. A session that skips these is not a usable
#: session — it is a handle that silently drops the events its caller is waiting for.
DEFAULT_DOMAINS = ("Page", "DOM", "Runtime", "Network")


class State(str, Enum):
    """Typed session state (TODO 10). Replaces v1's "does this string look stale?"."""

    ATTACHED = "attached"
    TARGET_MISSING = "target_missing"
    SESSION_STALE = "session_stale"
    RENDERER_UNRESPONSIVE = "renderer_unresponsive"
    BROWSER_DISCONNECTED = "browser_disconnected"


#: One vocabulary, two views: a dead state maps to the outcome class a caller recovers on.
STATE_CLASS = {
    State.TARGET_MISSING: Class.TARGET_GONE,
    State.SESSION_STALE: Class.SESSION_STALE,
    State.RENDERER_UNRESPONSIVE: Class.RENDERER_UNRESPONSIVE,
    State.BROWSER_DISCONNECTED: Class.BROWSER_DISCONNECTED,
}


@dataclass(slots=True)
class Session:
    target_id: str
    session_id: str
    state: State = State.ATTACHED
    domains: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""

    @property
    def live(self) -> bool:
        return self.state is State.ATTACHED

    def to_json(self) -> dict[str, Any]:
        d = {"target_id": self.target_id, "session_id": self.session_id,
             "state": self.state.value}
        if self.reason:
            d["reason"] = self.reason
        return d


class SessionRegistry:
    """One session per target, never pooled, never shared across targets (D1).

    Locking is **per target**, not global: two clients attaching to two different tabs must
    proceed concurrently — that is issue #375's subagent case. Two clients attaching to the
    *same* tab must produce one session, not two, which is what the per-target lock buys.
    """

    def __init__(self, conn: Connection, *, journal: Journal | None = None,
                 domains: tuple[str, ...] = DEFAULT_DOMAINS):
        self._conn = conn
        self._j = journal or Journal(None)
        self._domains = domains
        self._sessions: dict[str, Session] = {}
        # The injected scripts and binding are properties of a CDP *session*, not of the
        # short-lived ``bh`` process that happens to use it. Keying the prepared generation
        # by target lets the persistent daemon pay this setup once while still reinstalling
        # it after a stale session is replaced.
        self._runtime_sessions: dict[str, str] = {}
        #: targetId -> sessionId for children the BROWSER attached (OOPIFs, under
        #: Target.setAutoAttach). Out-of-process iframes are never listed by
        #: `Target.getTargets` and `attachToTarget` rejects their id, so auto-attach is
        #: the only way to reach one — which makes the browser a session producer we do
        #: not control. Booking it here keeps `ready_session()` the only place that turns
        #: a session into a *usable* one, which is what item 9 was actually protecting.
        self._adopted: dict[str, str] = {}
        self._by_session: dict[str, str] = {}      # sessionId → targetId, for event routing
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        conn.subscribe(self.on_event)

    # -- the one producer --------------------------------------------------

    def ready_session(self, target_id: str) -> Session:
        """**The only function in the tree that produces a usable session.**

        Attach, enable domains, register — in that order, under the target's lock. Every
        path reaches a session through here: first attach, tab switch, and lazy attach are
        the same code, so they cannot drift apart the way v1's three producers did.

        Idempotent: a live session is returned as-is, without a round trip.
        """
        lock = self._lock_for(target_id)
        with lock:
            return self._ready_session_locked(target_id)

    def _ready_session_locked(self, target_id: str) -> Session:
        """Produce a session while the caller owns this target's lock."""
        with self._guard:
            existing = self._sessions.get(target_id)
        if existing is not None and existing.live:
            return existing

        with self._guard:
            adopted = self._adopted.pop(target_id, None)
        if adopted is not None:
            # The browser already handed us this one; enabling domains is what makes
            # it a session by our definition, and it still happens here and nowhere else.
            session = Session(target_id=target_id, session_id=adopted,
                              domains=self._enable_domains(adopted, self._domains))
            with self._guard:
                self._sessions[target_id] = session
                self._by_session[adopted] = target_id
            self._j.write("daemon", event="adopted", **session.to_json())
            return session

        try:
            result = self._conn.request(
                "Target.attachToTarget", {"targetId": target_id, "flatten": True}
            )
        except HarnessError as e:
            # Attaching to a tab that closed is `TARGET_GONE`, not a mystery.
            if e.cls in (Class.TARGET_GONE, Class.CDP_ERROR):
                raise self._dead(target_id, State.TARGET_MISSING, e.args[0]) from e
            raise

        session_id = result.get("sessionId")
        if not session_id:
            raise self._dead(target_id, State.TARGET_MISSING,
                             "attachToTarget returned no sessionId")

        enabled = self._enable_domains(session_id, self._domains_for(target_id))
        session = Session(target_id=target_id, session_id=session_id,
                          domains=enabled)
        with self._guard:
            self._sessions[target_id] = session
            self._by_session[session_id] = target_id
        self._j.write("daemon", event="attached", **session.to_json())
        return session

    def _domains_for(self, target_id: str) -> tuple[str, ...]:
        """Workers have no Page or DOM domains; pages retain the full contract."""
        try:
            info = self._conn.request("Target.getTargetInfo", {"targetId": target_id}) \
                .get("targetInfo") or {}
        except HarnessError:
            info = {}
        kind = str(info.get("type") or "page")
        if kind in {"service_worker", "shared_worker", "worker"}:
            return tuple(domain for domain in self._domains if domain == "Runtime")
        return self._domains

    def _enable_domains(self, session_id: str, domains: tuple[str, ...]) -> tuple[str, ...]:
        """The single place a CDP domain is ever enabled.

        `grep -rn '\\.enable' harness/` must find this function and nothing else — the test
        asserts it. That grep is the whole point of TODO 9: v1's tab-switch path attached
        without enabling, so `Network.*` events stopped arriving with no error anywhere.
        """
        enabled: list[str] = []
        for domain in domains:
            self._conn.request(f"{domain}.enable", session_id=session_id, timeout=10.0)
            enabled.append(domain)
        # Session setup lives here too, for the same reason the enables do: a session
        # without lifecycle events silently breaks every event-driven wait (D13).
        if "Page" in domains:
            self._conn.request("Page.setLifecycleEventsEnabled", {"enabled": True},
                               session_id=session_id, timeout=10.0)
        return tuple(enabled)

    # -- the one consumer boundary ----------------------------------------

    def ensure_live(self, target_id: str) -> Session:
        """Return a live session or raise its typed error (TODO 10).

        A dictionary lookup, not a probe: the browser already told us when the target died.

        One dead state is not final. `SESSION_STALE` means the browser detached the
        *session* — the tab itself may be perfectly alive, and a session is only a lease
        on a target, so the recovery is to take a new lease: re-attach through the one
        producer and carry on. This is safe here precisely because callers hold TARGET
        ids: the replacement session is for the same target by construction, so there is
        no tab the caller could be silently redirected to (the redirect hazard is what
        browser-use PR 618 needed a session-replacement map and an explicit-caller
        refusal to manage — v1's callers hold session ids). `ready_session` fails closed
        with `TARGET_MISSING` when the tab really is gone, so a wrong guess about
        liveness cannot fabricate a tab. The other dead states stay final: a destroyed
        target has nothing to re-attach to, and a crashed renderer needs a reload
        decision the caller owns.
        """
        lock = self._lock_for(target_id)
        with lock:
            with self._guard:
                session = self._sessions.get(target_id)
            if session is None:
                return self._ready_session_locked(target_id)
            if session.live:
                return session
            if session.state is State.SESSION_STALE:
                self._j.write("daemon", event="session_reattached", target=target_id,
                              stale_session=session.session_id)
                # Remove exactly the stale generation observed above. Keeping the
                # check and replacement under the target lock prevents a second caller
                # from deleting the fresh session installed by the first caller.
                with self._guard:
                    if self._sessions.get(target_id) is session:
                        self._sessions.pop(target_id, None)
                        self._by_session.pop(session.session_id, None)
                return self._ready_session_locked(target_id)
            raise HarnessError.of(Outcome(
                ok=False, cls=STATE_CLASS[session.state], detail=session.reason,
                observed={"target_id": target_id, "session_id": session.session_id,
                          "state": session.state.value},
            ))

    def prepare_runtime(self, target_id: str) -> Session:
        """Install the safety/runtime scripts once for the live session generation.

        A benchmark command is a fresh Python process, but the daemon and its registered
        CDP session persist. Installing two document-start scripts and one binding in every
        process was therefore pure duplicate work. The target lock makes concurrent first
        users share one installation; the cached session id makes stale-session recovery
        re-arm the replacement generation automatically.
        """
        lock = self._lock_for(target_id)
        with lock:
            session = self._ready_session_locked(target_id)
            with self._guard:
                if self._runtime_sessions.get(target_id) == session.session_id:
                    return session

            # Local import avoids making the connection layer import page operations while
            # modules are initialising. At call time page.py is fully loaded in clients; in
            # the daemon this is the first operation that needs the script assets.
            from harness.ops.page import BINDING, RUNTIME_JS, SAFETY_JS, WORLD

            # Experiment switches read here, in the daemon, because this is where the
            # document-start scripts are installed. `BH_DEEP_QUERY=0` restores the shallow
            # (pre-shadow-DOM) query in both worlds; `BH_BLOCK_URLS` is a comma-separated
            # `Network.setBlockedURLs` pattern list applied to every prepared session.
            shallow = "1" if os.environ.get("BH_DEEP_QUERY", "1").strip() == "0" else "0"
            self._conn.request(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": SAFETY_JS.replace("'__SHALLOW__'", f"'{shallow}'"),
                 "runImmediately": True},
                session_id=session.session_id,
                timeout=10.0,
            )
            self._conn.request(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": RUNTIME_JS.replace("'__SHALLOW__'", f"'{shallow}'"),
                    "worldName": WORLD,
                    "runImmediately": True,
                },
                session_id=session.session_id,
                timeout=10.0,
            )
            blocked = [p.strip() for p in os.environ.get("BH_BLOCK_URLS", "").split(",")
                       if p.strip()]
            if blocked:
                try:
                    self._conn.request("Network.setBlockedURLs", {"urls": blocked},
                                       session_id=session.session_id, timeout=10.0)
                except HarnessError as error:
                    self._j.write("daemon", event="block_urls_failed", target_id=target_id,
                                  error=f"{type(error).__name__}: {str(error)[:120]}")
            binding = True
            try:
                self._conn.request(
                    "Runtime.addBinding",
                    {"name": BINDING, "executionContextName": WORLD},
                    session_id=session.session_id,
                    timeout=10.0,
                )
            except HarnessError:
                # This binding only wakes event-driven waits early. The waits retain their
                # bounded timeout fallback, matching the previous best-effort behavior.
                binding = False
            with self._guard:
                self._runtime_sessions[target_id] = session.session_id
            self._j.write(
                "daemon",
                event="runtime_prepared",
                target_id=target_id,
                session_id=session.session_id,
                binding=binding,
            )
            return session

    def ensure_domains(self, target_id: str, domains: tuple[str, ...]) -> Session:
        """Enable additional idempotent domains once on the live session."""
        lock = self._lock_for(target_id)
        with lock:
            session = self._ready_session_locked(target_id)
            missing = tuple(domain for domain in domains if domain not in session.domains)
            if not missing:
                return session
            enabled = self._enable_domains(session.session_id, missing)
            session.domains = tuple(dict.fromkeys((*session.domains, *enabled)))
            self._j.write(
                "daemon",
                event="domains_enabled",
                target_id=target_id,
                domains=list(enabled),
            )
            return session

    def mark(self, target_id: str, state: State, reason: str = "") -> None:
        """Record that a session died, and why. Idempotent."""
        with self._guard:
            session = self._sessions.get(target_id)
            if session is None or session.state is state:
                return
            session.state, session.reason = state, reason
        self._j.write("daemon", event="session_dead", target=target_id,
                      state=state.value, reason=reason)

    def forget(self, target_id: str) -> None:
        with self._guard:
            session = self._sessions.pop(target_id, None)
            self._runtime_sessions.pop(target_id, None)
            if session is not None:
                self._by_session.pop(session.session_id, None)

    # -- lifecycle events --------------------------------------------------

    def on_event(self, msg: dict[str, Any]) -> None:
        """Learn about death from the browser instead of from a failed command.

        Runs on the reader thread, so it only touches dictionaries under a lock.
        """
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "Target.attachedToTarget":
            info = params.get("targetInfo") or {}
            sid = params.get("sessionId")
            if sid and info.get("targetId") and info.get("type") == "iframe":
                # Record only. Enabling domains needs a round trip, and this runs on the
                # reader thread — which cannot dispatch the reply it would wait for.
                with self._guard:
                    self._adopted[info["targetId"]] = sid
        if method == "Target.detachedFromTarget":
            target = self._target_for(params.get("sessionId"))
            if target:
                self.mark(target, State.SESSION_STALE, "browser detached the session")
        elif method == "Target.targetDestroyed":
            target = params.get("targetId")
            if target:
                self.mark(target, State.TARGET_MISSING, "target destroyed")
        elif method == "Target.targetCrashed":
            target = params.get("targetId")
            if target:
                self.mark(target, State.RENDERER_UNRESPONSIVE,
                          f"renderer crashed: {params.get('reason', '')}".strip())
        elif method == "Inspector.targetCrashed":
            target = self._target_for(msg.get("sessionId"))
            if target:
                self.mark(target, State.RENDERER_UNRESPONSIVE, "renderer crashed")

    def disconnected(self, reason: str = "browser connection lost") -> None:
        """Every session dies with the connection — none of them outlive the websocket."""
        with self._guard:
            targets = list(self._sessions)
        for target_id in targets:
            self.mark(target_id, State.BROWSER_DISCONNECTED, reason)

    def discover(self) -> None:
        """Ask the browser to report target lifecycle. Without this there is no
        `targetDestroyed`, which is precisely the event v1 never subscribed to."""
        self._conn.request("Target.setDiscoverTargets", {"discover": True})

    # -- internals ---------------------------------------------------------

    def _lock_for(self, target_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(target_id, threading.Lock())

    def _target_for(self, session_id: str | None) -> str | None:
        if not session_id:
            return None
        with self._guard:
            return self._by_session.get(session_id)

    def _dead(self, target_id: str, state: State, reason: str) -> HarnessError:
        self.mark(target_id, state, reason)
        return HarnessError.of(Outcome(
            ok=False, cls=STATE_CLASS[state], detail=reason,
            observed={"target_id": target_id, "state": state.value},
        ))

    @property
    def live_targets(self) -> list[str]:
        with self._guard:
            return [t for t, s in self._sessions.items() if s.live]
