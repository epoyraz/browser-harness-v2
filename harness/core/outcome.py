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
    SIDE_EFFECT_REFUSED = "side_effect_refused"  # default-deny irreversible browser action
    RESOURCE_LIMIT = "resource_limit"          # a declared machine/session budget was exhausted
    RESOURCE_CLEANUP_FAILED = "resource_cleanup_failed"  # owned resource could not be released
    CANCELLED = "cancelled"                    # aggregate work stopped before this item began
    PROTOCOL_MISMATCH = "protocol_mismatch"    # client and daemon cannot safely communicate
    SKILL_INTEGRITY_FAILED = "skill_integrity_failed"  # body differs from indexed digest

    # session / target (D1, D11)
    TARGET_GONE = "target_gone"
    SESSION_STALE = "session_stale"
    RENDERER_UNRESPONSIVE = "renderer_unresponsive"

    #: The browser refused a command and `classify()` did not recognise which way. This is
    #: NOT a `misc` member: it is the honest floor of rule 1. Without it, an unrecognised
    #: CDP error would have to be reported as some *specific* cause we never verified.
    CDP_ERROR = "cdp_error"

    # page-level (D11, D15)
    NAVIGATION_FAILED = "navigation_failed"
    HTTP_ERROR = "http_error"                  # added for fetch_all (D0): a 404 inside a
                                               # batch is not a navigation and not a JS throw
    JS_EXCEPTION = "js_exception"
    NOT_SERIALIZABLE = "not_serializable"      # v1 returned None here, silently
    NO_OPTION_MATCH = "no_option_match"        # 249 prefixes, none matched the label
    #: A widget that cannot be set by value at all — an ARIA combobox built from divs.
    #: Distinct from NO_OPTION_MATCH because the recovery differs: click the control and
    #: pick from its popup, rather than supply a different label.
    NEEDS_INTERACTION = "needs_interaction"
    #: The write executed cleanly and the control refused or rewrote it — a masked phone
    #: field reverting to its `+41` prefix, a formatter normalising as you type. Reporting
    #: this as JS_EXCEPTION sent readers hunting a stack trace that never existed; the
    #: recovery is a different write mode, not a different script.
    VALUE_REJECTED = "value_rejected"
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
    #: For aggregates (rule 4): the per-item failures, still typed. A bare failed-count
    #: would force callers to re-parse the report to learn *why* — the `str` disease back.
    failures: list[Outcome] = field(default_factory=list)

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
        if self.failures:
            d["failures"] = [f.to_json() for f in self.failures]
        return d


class MappingOutcome(dict[str, Any]):
    """Outcome contract for helpers whose public payload was historically a mapping."""

    def __init__(self, outcome: Outcome):
        if not isinstance(outcome.value, dict):
            raise TypeError("MappingOutcome requires a dict value")
        super().__init__(outcome.value)
        self._outcome = outcome

    @property
    def value(self) -> MappingOutcome:
        return self

    @property
    def observed(self) -> MappingOutcome:
        return self

    def unwrap(self) -> MappingOutcome:
        if not self.ok:
            self._outcome.observed = dict(self)
            self._outcome.value = dict(self)
            raise HarnessError.of(self._outcome)
        return self

    def to_json(self) -> dict[str, Any]:
        data = self._outcome.to_json()
        data["observed"] = dict(self)
        return data

    def __getattr__(self, name: str) -> Any:
        try:
            outcome = object.__getattribute__(self, "_outcome")
        except AttributeError:
            raise AttributeError(name) from None
        return getattr(outcome, name)


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

    def outcome(self, value: Any = None, **observed: Any) -> Outcome:
        """PARTIAL when anything failed — never a bare success carrying a short list.
        `value` overrides the default (the successes) when the caller's natural payload
        is the full report, e.g. fill_form's per-field ok/got/want list."""
        seen = {"attempted": self.attempted, "succeeded": self.succeeded, "failed": self.failed,
                **observed}
        payload = self.results if value is None else value
        if self.complete:
            return Outcome(ok=True, value=payload, observed=seen)
        return Outcome(
            ok=False,
            cls=Class.PARTIAL,
            detail=f"{self.succeeded}/{self.attempted} succeeded",
            observed=seen,
            value=payload,               # partial results are still returned, just not as success
            failures=list(self.failures),
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
SideEffectRefused = _error("SideEffectRefused", Class.SIDE_EFFECT_REFUSED)
ResourceLimit = _error("ResourceLimit", Class.RESOURCE_LIMIT)
ResourceCleanupFailed = _error("ResourceCleanupFailed", Class.RESOURCE_CLEANUP_FAILED)
Cancelled = _error("Cancelled", Class.CANCELLED)
ProtocolMismatch = _error("ProtocolMismatch", Class.PROTOCOL_MISMATCH)
SkillIntegrityFailed = _error("SkillIntegrityFailed", Class.SKILL_INTEGRITY_FAILED)
TargetGone = _error("TargetGone", Class.TARGET_GONE)
SessionStale = _error("SessionStale", Class.SESSION_STALE)
RendererUnresponsive = _error("RendererUnresponsive", Class.RENDERER_UNRESPONSIVE)
CdpError = _error("CdpError", Class.CDP_ERROR)
NavigationFailed = _error("NavigationFailed", Class.NAVIGATION_FAILED)
HttpError = _error("HttpError", Class.HTTP_ERROR)
JsException = _error("JsException", Class.JS_EXCEPTION)
NotSerializable = _error("NotSerializable", Class.NOT_SERIALIZABLE)
NoOptionMatch = _error("NoOptionMatch", Class.NO_OPTION_MATCH)
NeedsInteraction = _error("NeedsInteraction", Class.NEEDS_INTERACTION)
ValueRejected = _error("ValueRejected", Class.VALUE_REJECTED)
NotAForm = _error("NotAForm", Class.NOT_A_FORM)
ElementGone = _error("ElementGone", Class.ELEMENT_GONE)
Partial = _error("Partial", Class.PARTIAL)
Timeout = _error("Timeout", Class.TIMEOUT)

_BY_CLASS: dict[Class, type[HarnessError]] = {
    e.cls: e for e in (
        EndpointUnreachable, Endpoint404, NoBrowserWindow, PermissionPending,
        WsRejectedUpstream, BrowserDisconnected, ScopeRefused, SideEffectRefused,
        ResourceLimit, ResourceCleanupFailed, Cancelled, ProtocolMismatch,
        SkillIntegrityFailed,
        TargetGone, SessionStale,
        RendererUnresponsive, CdpError, NavigationFailed, HttpError, JsException,
        NotSerializable, NoOptionMatch, NeedsInteraction, ValueRejected, NotAForm,
        ElementGone, Partial,
        Timeout,
    )
}


def ok(value: Any = None, **observed: Any) -> Outcome:
    return Outcome(ok=True, value=value, observed=observed)


def fail(cls: Class, detail: str = "", **observed: Any) -> Outcome:
    return Outcome(ok=False, cls=cls, detail=detail, observed=observed)
