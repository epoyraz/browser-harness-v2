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
            existing = self._sessions.get(target_id)
            if existing is not None and existing.live:
                return existing

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

            enabled = self._enable_domains(session_id)
            session = Session(target_id=target_id, session_id=session_id,
                              domains=enabled)
            with self._guard:
                self._sessions[target_id] = session
                self._by_session[session_id] = target_id
            self._j.write("daemon", event="attached", **session.to_json())
            return session

    def _enable_domains(self, session_id: str) -> tuple[str, ...]:
        """The single place a CDP domain is ever enabled.

        `grep -rn '\\.enable' harness/` must find this function and nothing else — the test
        asserts it. That grep is the whole point of TODO 9: v1's tab-switch path attached
        without enabling, so `Network.*` events stopped arriving with no error anywhere.
        """
        enabled: list[str] = []
        for domain in self._domains:
            self._conn.request(f"{domain}.enable", session_id=session_id, timeout=10.0)
            enabled.append(domain)
        # Session setup lives here too, for the same reason the enables do: a session
        # without lifecycle events silently breaks every event-driven wait (D13).
        self._conn.request("Page.setLifecycleEventsEnabled", {"enabled": True},
                           session_id=session_id, timeout=10.0)
        return tuple(enabled)

    # -- the one consumer boundary ----------------------------------------

    def ensure_live(self, target_id: str) -> Session:
        """Return a live session or raise its typed error (TODO 10).

        A dictionary lookup, not a probe: the browser already told us when the target died.
        """
        with self._guard:
            session = self._sessions.get(target_id)
        if session is None:
            return self.ready_session(target_id)
        if session.live:
            return session
        raise HarnessError.of(Outcome(
            ok=False, cls=STATE_CLASS[session.state], detail=session.reason,
            observed={"target_id": target_id, "session_id": session.session_id,
                      "state": session.state.value},
        ))

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
            if session is not None:
                self._by_session.pop(session.session_id, None)

    # -- lifecycle events --------------------------------------------------

    def on_event(self, msg: dict[str, Any]) -> None:
        """Learn about death from the browser instead of from a failed command.

        Runs on the reader thread, so it only touches dictionaries under a lock.
        """
        method = msg.get("method")
        params = msg.get("params") or {}
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
