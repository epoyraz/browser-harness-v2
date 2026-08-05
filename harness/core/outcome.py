"""The outcome contract (DESIGN.md D11).

v1 had exactly one error type and it was `str`: the daemon did `{"error": str(e)}`, the
client did `raise RuntimeError(...)`, and recovery string-matched Chrome's prose back out.
Every reworded Chrome message was a new bug.

Here an operation returns an *outcome*, typed at the boundary where the information still
exists. Four rules, each earned:

  1. Never invent a cause you did not verify   — `permission_pending` requires an observed
     prompt, not merely a timeout.
  2. Never discard a cause you were handed     — CDP's `errorText` reaches the caller.
  3. Define success                            — `ok` is explicit, so "no exception" can
     never be mistaken for success.
  4. Partial work is not success               — an operation over N items reports
     attempted/succeeded/failed, never just the successes.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Class(str, Enum):
    """Closed enum. Recovery branches on this, never on prose, so Chrome may reword freely.

    Adding a member is a deliberate act: it means a caller can now distinguish a case it
    could not before. Resist a `misc` member — that is how `str` comes back.
    """

    OK = "ok"

    # connection / endpoint (DESIGN.md D8, D10, D11)
    ENDPOINT_UNREACHABLE = "endpoint_unreachable"
    ENDPOINT_404 = "endpoint_404"              # Chrome 147 disables /json/* on the default profile
    NO_BROWSER_WINDOW = "no_browser_window"    # running with zero windows — no popup can exist
    PERMISSION_PENDING = "permission_pending"  # only when a prompt was actually observed
    WS_REJECTED_UPSTREAM = "ws_rejected_upstream"
    BROWSER_DISCONNECTED = "browser_disconnected"
    SCOPE_REFUSED = "scope_refused"            # a pinned binding declined to widen (#479)

    # session / target (D1, D11)
    TARGET_GONE = "target_gone"
    SESSION_STALE = "session_stale"
    RENDERER_UNRESPONSIVE = "renderer_unresponsive"

    # page-level (D11, D15)
    NAVIGATION_FAILED = "navigation_failed"
    JS_EXCEPTION = "js_exception"
    NOT_SERIALIZABLE = "not_serializable"      # v1 returned None here, silently
    NO_OPTION_MATCH = "no_option_match"        # 249 prefixes, none matched the label
    NOT_A_FORM = "not_a_form"                  # form-identity verdict failed (D15)
    ELEMENT_GONE = "element_gone"

    # aggregate (D11, rule 4)
    PARTIAL = "partial"

    TIMEOUT = "timeout"


#: Classes worth retrying unchanged. Stated by the party that knows, never guessed
#: by the caller.
RETRYABLE = frozenset({
    Class.TIMEOUT,
    Class.RENDERER_UNRESPONSIVE,
    Class.SESSION_STALE,
    Class.BROWSER_DISCONNECTED,
})


@dataclass(slots=True)
class Outcome:
    """What an operation returns. `detail` is for humans and is never parsed."""

    ok: bool
    cls: Class = Class.OK
    detail: str = ""
    observed: dict[str, Any] = field(default_factory=dict)
    value: Any = None
    id: str = ""
    ts: float = field(default_factory=time.time)

    @property
    def retryable(self) -> bool:
        return self.cls in RETRYABLE

    def unwrap(self) -> Any:
        """Value, or raise the typed error. Explicit — callers that want the outcome keep it."""
        if not self.ok:
            raise HarnessError.of(self)
        return self.value

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"ok": self.ok, "class": self.cls.value, "ts": round(self.ts, 3)}
        if self.detail:
            d["detail"] = self.detail
        if self.observed:
            d["observed"] = self.observed
        if self.id:
            d["id"] = self.id
        if not self.ok:
            d["retryable"] = self.retryable
        return d


@dataclass(slots=True)
class Tally:
    """Rule 4: an operation over N items reports all three counts.

    Silent truncation reads as "covered everything" when it did not — an unbounded fan-out
    once returned 163 of ~300 results with no error raised.
    """

    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    results: list[Any] = field(default_factory=list)
    failures: list[Outcome] = field(default_factory=list)

    def record(self, outcome: Outcome) -> None:
        self.attempted += 1
        if outcome.ok:
            self.succeeded += 1
            self.results.append(outcome.value)
        else:
            self.failed += 1
            self.failures.append(outcome)

    @property
    def complete(self) -> bool:
        return self.attempted > 0 and self.failed == 0

    def outcome(self, **observed: Any) -> Outcome:
        """PARTIAL when anything failed — never a bare success carrying a short list."""
        seen = {"attempted": self.attempted, "succeeded": self.succeeded, "failed": self.failed,
                **observed}
        if self.complete:
            return Outcome(ok=True, value=self.results, observed=seen)
        return Outcome(
            ok=False,
            cls=Class.PARTIAL,
            detail=f"{self.succeeded}/{self.attempted} succeeded",
            observed=seen,
            value=self.results,          # partial results are still returned, just not as success
        )


class HarnessError(Exception):
    """Base for typed errors. Carries the outcome, so `e.observed` beats parsing `str(e)`."""

    cls: Class = Class.TIMEOUT

    def __init__(self, detail: str = "", **observed: Any):
        self.outcome = Outcome(ok=False, cls=self.cls, detail=detail, observed=observed)
        super().__init__(f"{self.cls.value}: {detail}" if detail else self.cls.value)

    @property
    def observed(self) -> dict[str, Any]:
        return self.outcome.observed

    @property
    def retryable(self) -> bool:
        return self.outcome.retryable

    @staticmethod
    def of(outcome: Outcome) -> HarnessError:
        exc = _BY_CLASS.get(outcome.cls, HarnessError)(outcome.detail)
        exc.outcome = outcome
        return exc


def _error(name: str, cls: Class) -> type[HarnessError]:
    return type(name, (HarnessError,), {"cls": cls, "__doc__": f"Typed error for {cls.value}."})


EndpointUnreachable = _error("EndpointUnreachable", Class.ENDPOINT_UNREACHABLE)
Endpoint404 = _error("Endpoint404", Class.ENDPOINT_404)
NoBrowserWindow = _error("NoBrowserWindow", Class.NO_BROWSER_WINDOW)
PermissionPending = _error("PermissionPending", Class.PERMISSION_PENDING)
WsRejectedUpstream = _error("WsRejectedUpstream", Class.WS_REJECTED_UPSTREAM)
BrowserDisconnected = _error("BrowserDisconnected", Class.BROWSER_DISCONNECTED)
ScopeRefused = _error("ScopeRefused", Class.SCOPE_REFUSED)
TargetGone = _error("TargetGone", Class.TARGET_GONE)
SessionStale = _error("SessionStale", Class.SESSION_STALE)
RendererUnresponsive = _error("RendererUnresponsive", Class.RENDERER_UNRESPONSIVE)
NavigationFailed = _error("NavigationFailed", Class.NAVIGATION_FAILED)
JsException = _error("JsException", Class.JS_EXCEPTION)
NotSerializable = _error("NotSerializable", Class.NOT_SERIALIZABLE)
NoOptionMatch = _error("NoOptionMatch", Class.NO_OPTION_MATCH)
NotAForm = _error("NotAForm", Class.NOT_A_FORM)
ElementGone = _error("ElementGone", Class.ELEMENT_GONE)
Partial = _error("Partial", Class.PARTIAL)
Timeout = _error("Timeout", Class.TIMEOUT)

_BY_CLASS: dict[Class, type[HarnessError]] = {
    e.cls: e for e in (
        EndpointUnreachable, Endpoint404, NoBrowserWindow, PermissionPending,
        WsRejectedUpstream, BrowserDisconnected, ScopeRefused, TargetGone, SessionStale,
        RendererUnresponsive, NavigationFailed, JsException, NotSerializable, NoOptionMatch,
        NotAForm, ElementGone, Partial, Timeout,
    )
}


def ok(value: Any = None, **observed: Any) -> Outcome:
    return Outcome(ok=True, value=value, observed=observed)


def fail(cls: Class, detail: str = "", **observed: Any) -> Outcome:
    return Outcome(ok=False, cls=cls, detail=detail, observed=observed)
