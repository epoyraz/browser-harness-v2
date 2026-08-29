"""Bounded parallel work over tabs in one Chrome instance.

The hard ceilings are ten tabs and zero additional browsers. Optional browser contexts
isolate worker cookies/storage without multiplying Chrome processes. A fixed worker loop
claims items lazily, so cancellation prevents queued work from starting instead of merely
setting a flag after an executor has already submitted the full input.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from harness.core.outcome import Class, HarnessError
from harness.core.resources import ResourceLedger

MAX_WORKERS = 10
DEFAULT_WORKERS = 8

# A worker tab is not reusable until its opener tree has stayed quiet for a short bounded
# window.  Chrome can announce a popup after the item function has returned (measured at
# 150 ms), so a single immediate Target.getTargets snapshot is not an isolation boundary.
# The event-driven wait adds only the quiet window, never an unconditional max-duration
# sleep, and the cap prevents a page that continuously opens targets from holding a worker.
# 200 → 50 ms (2026-08-29): two replicates of the 100-posting corpus at 50 ms — 320 items —
# announced zero late popups, and the four opener descendants that did appear were all
# caught. Cleanup dropped from 20.9 s to 6.2 s per run; forms were unchanged (+3/−0, +5/−0,
# +2/−3 against adjacent controls). `cleanup_descendants` in the run summary is the
# tripwire: a descendant found *after* the window would show up as a leaked tab there.
POPUP_CLEANUP_QUIET = max(0.0, float(os.environ.get("BH_POPUP_QUIET_MS", "50") or 50) / 1000)
POPUP_CLEANUP_MAX_WAIT = 0.8


class CancelToken:
    """Shared cooperative cancellation with an optional whole-run deadline."""

    def __init__(self, *, timeout: float | None = None):
        self._event = threading.Event()
        self.deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        self.reason: str | None = None

    def cancel(self, reason: str | None = None) -> None:
        if reason and self.reason is None:
            self.reason = reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set() or (
            self.deadline is not None and time.monotonic() >= self.deadline)


def _is_browser_gone(error: Exception) -> bool:
    """True for the one failure that is the whole run's, not the item's."""
    if isinstance(error, HarnessError):
        return error.outcome.cls is Class.BROWSER_DISCONNECTED
    return type(error).__name__ == "BrowserDisconnected"


def _failure(item: Any, error: Exception) -> dict[str, Any]:
    if isinstance(error, HarnessError):
        return {"item": item, "ok": False, "class": error.outcome.cls.value,
                "error": str(error)[:200], "outcome": error.outcome.to_json()}
    return {"item": item, "ok": False, "class": type(error).__name__,
            "error": str(error)[:200]}


def _cleanup_failed(record: dict[str, Any], failures: list[dict[str, Any]]) -> None:
    if not failures:
        return
    record["cleanup_ok"] = False
    record["cleanup_failures"] = failures
    if record.get("ok"):
        record["ok"] = False
        record["class"] = Class.RESOURCE_CLEANUP_FAILED.value
        record["error"] = "owned browser resources could not be released"


def _owned_tab_descendants(session: Any, root: str) -> tuple[list[str], bool]:
    """Return live page targets causally opened by one owned worker tab.

    Chrome retains ``openerId`` even for ``rel=noopener`` targets while the opener is
    alive (measured with ``canAccessOpener=false``).  Following that chain is therefore
    both safer and more complete than diffing the global target list: concurrent workers'
    tabs and pre-existing user tabs can never be mistaken for this worker's resources.
    """
    infos = session.targets()
    live = {str(info.get("targetId")) for info in infos if info.get("targetId")}
    owned = {root}
    descendants: list[str] = []
    while True:
        found = [
            str(info["targetId"])
            for info in infos
            if info.get("type") in {"page", "tab"}
            and info.get("targetId")
            and str(info["targetId"]) not in owned
            and str(info.get("openerId") or "") in owned
        ]
        if not found:
            break
        owned.update(found)
        descendants.extend(found)
    return descendants, root in live


def _owned_tab_descendants_after_quiet(
    session: Any,
    root: str,
) -> tuple[list[str], bool]:
    """Observe the root's opener tree until it is briefly quiet, then snapshot it.

    ``Target.targetCreated`` is browser-level, so session ids cannot establish ownership.
    The opener chain can: only a page/tab whose ``openerId`` is the root or an already
    observed descendant extends the wait.  Foreign workers and the user's own tabs never
    delay or enter this cleanup set.

    Small test doubles and alternate Session implementations may not expose event
    subscription.  They retain the original immediate, causally scoped snapshot rather
    than failing cleanup merely because the optional observation channel is absent.
    """
    conn = getattr(session, "conn", None)
    if conn is None or not callable(getattr(conn, "subscribe", None)) \
            or not callable(getattr(conn, "unsubscribe", None)):
        return _owned_tab_descendants(session, root)

    cond = threading.Condition()
    owned = {root}
    started = time.monotonic()
    last_owned_event = started
    announced_after_ms: list[float] = []
    #: Set when the browser says something happened that the seed snapshot cannot already
    #: describe: a new target joined the opener tree, or one we own went away.
    moved = False
    #: True until the seed snapshot has been folded into `owned`.
    seeding = True

    def observe(msg: dict[str, Any]) -> None:
        nonlocal last_owned_event, moved, seeding
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "Target.targetDestroyed":
            with cond:
                # While the seed snapshot is still in flight, ownership is not yet known:
                # a destroyed descendant would not be in `owned` and a grandchild's opener
                # would not be either, so both would be judged irrelevant and the stale
                # snapshot returned. Anything arriving in that window forces the re-read.
                if seeding or str(params.get("targetId") or "") in owned:
                    moved = True       # the seed lists a tab that no longer needs closing
            return
        if method != "Target.targetCreated":
            return
        info = params.get("targetInfo") or {}
        target_id = str(info.get("targetId") or "")
        opener_id = str(info.get("openerId") or "")
        if info.get("type") not in {"page", "tab"} or not target_id:
            return
        with cond:
            if opener_id not in owned:
                if seeding:
                    moved = True       # its opener may be in the snapshot still in flight
                return
            owned.add(target_id)
            last_owned_event = time.monotonic()
            # How late a popup arrives is the only thing that can size POPUP_CLEANUP_QUIET,
            # and it is unrecorded. Over 100 postings the window cost 20.4s of wall clock
            # and observed *zero* descendants — which proves the price and says nothing at
            # all about the risk. A window cannot be shortened on the evidence of an event
            # that never fired; this is what makes the next attempt evidence-based.
            announced_after_ms.append(round((last_owned_event - started) * 1000, 1))
            moved = True
            cond.notify_all()

    conn.subscribe(observe)
    try:
        # Seed ownership from targets already present when cleanup begins.  Subscribing
        # first closes the gap between this snapshot and the quiet-window wait.
        descendants, root_live = _owned_tab_descendants(session, root)
        with cond:
            owned.update(descendants)
            seeding = False

        deadline = started + POPUP_CLEANUP_MAX_WAIT
        while True:
            with cond:
                now = time.monotonic()
                until = min(last_owned_event + POPUP_CLEANUP_QUIET, deadline)
                if now >= until:
                    break
                cond.wait(until - now)

        # Re-snapshot only when the browser reported something the seed cannot describe.
        # Ownership is derived from the opener chain, and the two events that can change
        # it — a target joining the tree, a target we own disappearing — were both being
        # watched throughout the wait. Without either, a second `Target.getTargets` is
        # guaranteed to return what the seed already holds. Measured on the 2026-08-25
        # corpus: 100 items announced 2 descendants between them, so 98 of 100 items paid
        # that round trip to be told nothing had changed.
        #
        # When something did move, the authoritative snapshot is still taken while
        # subscribed, so a target announced during the wait is either present in it or has
        # already disappeared and no longer needs cleanup.
        with cond:
            settled = not moved
            latencies = list(announced_after_ms)
        if latencies:
            session.journal.write("note", event="popup_announced",
                                  after_ms=latencies, quiet_window_ms=POPUP_CLEANUP_QUIET * 1000)
        if settled:
            return descendants, root_live
        return _owned_tab_descendants(session, root)
    finally:
        conn.unsubscribe(observe)


def _cleanup_observation_failure(session: Any, kind: str, identifier: str,
                                 error: Exception) -> dict[str, Any]:
    failure = {"kind": kind, "identifier": identifier,
               "error": f"{type(error).__name__}: {str(error)[:200]}"}
    session.journal.write("note", event="resource_cleanup_failed",
                          resource_kind=kind, identifier=identifier,
                          error=failure["error"])
    return failure


def parallel(session: Any, items: Iterable[Any], fn: Callable[[Any], Any], *,
             workers: int = 0, reuse_tabs: bool = True, isolated: bool = False,
             own_window: bool | None = None,
             worker_limit: int = MAX_WORKERS,
             timeout: float | None = None, token: CancelToken | None = None,
             progress: Callable[[int, int, dict[str, Any]], None] | None = None,
             events: Callable[[dict[str, Any]], None] | None = None,
             item_id: Callable[[Any], str] | None = None,
             ) -> list[dict[str, Any]]:
    """Run ``fn(item)`` with a bounded worker-tab pool and return ordered records.

    ``isolated=True`` gives every worker its own incognito browser context while still
    using the same Chrome process. ``reuse_tabs=False`` closes each tab immediately after
    its item; it no longer accumulates one tab per input. Cancellation is cooperative at
    item boundaries: active CDP calls finish under their own timeout, but no queued item
    starts after the token or whole-run deadline fires. The default limit remains ten;
    callers must explicitly raise ``worker_limit`` for larger stress tests.
    """
    todo: Sequence[Any] = list(items)
    if not todo:
        return []
    worker_count = workers or int(os.environ.get("BH_WORKERS") or 0) or DEFAULT_WORKERS
    worker_count = max(1, min(worker_count, len(todo), max(1, worker_limit)))
    windowed = (bool(own_window) if own_window is not None
                else os.environ.get("BH_PARALLEL_OWN_WINDOW", "").strip().lower() in ("1", "true", "yes"))
    # Own windows are tiled across the screen unless BH_PARALLEL_TILE=0: visible to the
    # user, and not occluding each other (occluded windows stop painting on Windows).
    tiled = os.environ.get("BH_PARALLEL_TILE", "1").strip().lower() not in ("0", "false", "no")
    cancel = token or CancelToken(timeout=timeout)
    if token is not None and timeout is not None:
        deadline = time.monotonic() + max(0.0, timeout)
        cancel.deadline = deadline if cancel.deadline is None else min(cancel.deadline, deadline)

    records: list[dict[str, Any] | None] = [None] * len(todo)
    claim_lock = threading.Lock()
    progress_lock = threading.Lock()
    next_index = 0
    completed = 0
    active = 0
    run_started = time.perf_counter()

    def identity(index: int, item: Any) -> str:
        if item_id is not None:
            return str(item_id(item))
        if isinstance(item, dict):
            for key in ("job_id", "id", "key"):
                if item.get(key) is not None:
                    return str(item[key])
        return str(index)

    def emit(event: dict[str, Any]) -> None:
        session.journal.write("note", event=f"parallel_item_{event['state']}", **event)
        if events is not None:
            try:
                events(dict(event))
            except Exception as error:  # noqa: BLE001 — reporting cannot break work
                session.journal.write("note", event="parallel_event_failed",
                                      error=f"{type(error).__name__}: {str(error)[:200]}")

    def claim() -> tuple[int, Any] | None:
        nonlocal next_index
        with claim_lock:
            if cancel.cancelled or next_index >= len(todo):
                return None
            index = next_index
            next_index += 1
            return index, todo[index]

    def report(index: int, record: dict[str, Any]) -> None:
        nonlocal completed
        records[index] = record
        if progress is None:
            return
        with progress_lock:
            completed += 1
            done = completed
        try:
            progress(done, len(todo), record)
        except Exception as error:  # noqa: BLE001 — reporting cannot break browser work
            session.journal.write("note", event="parallel_progress_failed",
                                  error=f"{type(error).__name__}: {str(error)[:200]}")

    def worker(worker_id: int) -> None:
        nonlocal active
        ledger = ResourceLedger(journal=getattr(session, "journal", None))
        worker_context: str | None = None
        worker_tab: str | None = None
        handled: list[int] = []
        try:
            while (claimed := claim()) is not None:
                index, item = claimed
                claimed_at = time.perf_counter()
                safe_id = identity(index, item)
                handled.append(index)
                item_context: str | None = None
                item_tab: str | None = None
                target_id: str | None = None
                cleanup_target_query_ms: float | None = None
                cleanup_descendants = 0
                record: dict[str, Any]
                with progress_lock:
                    active += 1
                    active_at_start = active
                emit({"state": "started", "item_id": safe_id, "item_index": index,
                      "worker_id": worker_id, "active": active_at_start,
                      "offset_ms": round((claimed_at - run_started) * 1000, 1)})
                try:
                    if isolated and (worker_context is None or not reuse_tabs):
                        item_context = session.new_context()
                        ledger.acquire("browser_context", item_context,
                                       lambda cid=item_context: session.close_context(cid))
                        if reuse_tabs:
                            worker_context = item_context
                    context_id = worker_context if reuse_tabs else item_context
                    if worker_tab is None or not reuse_tabs:
                        tab = (session.new_tab(context_id=context_id, new_window=True)
                               if windowed else session.new_tab(context_id=context_id))
                        item_tab = tab.target_id
                        if windowed and tiled:
                            try:
                                session.place_window(item_tab, slot=worker_id, slots=worker_count)
                            except Exception as error:  # noqa: BLE001 — placement is best effort
                                session.journal.write("note", event="window_place_failed",
                                                      worker_id=worker_id, error=str(error)[:120])
                        ledger.acquire("tab", item_tab,
                                       lambda tid=item_tab: session.close_tab(
                                           tid, wait=not reuse_tabs))
                        if reuse_tabs:
                            worker_tab = item_tab
                    else:
                        session.use_tab(worker_tab)
                    target_id = worker_tab or item_tab
                    with session.journal.bind(
                            item_id=safe_id, item_index=index, worker_id=worker_id,
                            target_id=target_id, browser_context_id=context_id):
                        record = {"item": item, "ok": True, "value": fn(item)}
                except Exception as error:  # noqa: BLE001 — one page must not erase siblings
                    record = _failure(item, error)
                    # A dead browser is not one page's failure. Left alone, every worker
                    # keeps claiming items and each one fails instantly on `new_tab`, so a
                    # 500-item run reported 265 `browser_disconnected` results in seconds
                    # with no work behind them (measured 2026-08-29). Stop claiming; the
                    # unstarted items are reported as such, with the reason on them.
                    if _is_browser_gone(error) and os.environ.get(
                            "BH_PARALLEL_STOP_ON_DISCONNECT", "1").strip() != "0":
                        cancel.cancel(reason="browser_disconnected")
                finally:
                    failures = []
                    root_tab = worker_tab or item_tab
                    if root_tab is not None:
                        cleanup_query_started = time.perf_counter()
                        try:
                            descendants, root_live = _owned_tab_descendants_after_quiet(
                                session, root_tab)
                        except Exception as error:  # noqa: BLE001 — observable cleanup
                            failures.append(_cleanup_observation_failure(
                                session, "tab_descendants", root_tab, error))
                        else:
                            cleanup_descendants = len(descendants)
                            # Deepest/newest targets close first.  A popup may itself have
                            # opened an authentication or ATS child during this item.
                            for descendant in reversed(descendants):
                                ledger.acquire(
                                    "tab", descendant,
                                    lambda tid=descendant: session.close_tab(tid),
                                )
                                if failure := ledger.release("tab", descendant):
                                    failures.append(failure)
                            if reuse_tabs and root_live:
                                try:
                                    session.use_tab(root_tab)
                                except Exception as error:  # noqa: BLE001 — observable cleanup
                                    failures.append(_cleanup_observation_failure(
                                        session, "tab_cursor", root_tab, error))
                            elif reuse_tabs:
                                # The page replaced/closed its opener. Create a fresh owned
                                # worker tab for the next item instead of reusing a dead id.
                                worker_tab = None
                        finally:
                            cleanup_target_query_ms = round(
                                (time.perf_counter() - cleanup_query_started) * 1000, 1)
                    if (not reuse_tabs and item_tab is not None
                            and (failure := ledger.release("tab", item_tab))):
                        failures.append(failure)
                    if (not reuse_tabs and item_context is not None
                            and (failure := ledger.release("browser_context", item_context))):
                        failures.append(failure)
                    _cleanup_failed(record, failures)
                completed_at = time.perf_counter()
                with progress_lock:
                    active -= 1
                    active_after = active
                record["telemetry"] = {
                    "item_id": safe_id, "item_index": index, "worker_id": worker_id,
                    "target_id": target_id,
                    "browser_context_id": worker_context if reuse_tabs else item_context,
                    "cleanup_target_query_ms": cleanup_target_query_ms,
                    "cleanup_descendants": cleanup_descendants,
                    "queued_ms": round((claimed_at - run_started) * 1000, 1),
                    "duration_ms": round((completed_at - claimed_at) * 1000, 1),
                    "completed_ms": round((completed_at - run_started) * 1000, 1),
                    "active_at_start": active_at_start, "active_after": active_after,
                }
                emit({"state": "completed", "item_id": safe_id, "item_index": index,
                      "worker_id": worker_id, "active": active_after,
                      "offset_ms": record["telemetry"]["completed_ms"],
                      "duration_ms": record["telemetry"]["duration_ms"],
                      "ok": bool(record.get("ok"))})
                report(index, record)
        finally:
            failures = ledger.cleanup()
            if failures and handled:
                record = records[handled[-1]]
                if record is not None:
                    _cleanup_failed(record, failures)

    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="bh-par") as pool:
        futures = [pool.submit(worker, worker_id) for worker_id in range(worker_count)]
        for future in futures:
            future.result()

    if cancel.reason:
        reason = cancel.reason
    elif cancel.deadline is not None and time.monotonic() >= cancel.deadline:
        reason = "deadline"
    else:
        reason = "cancelled"
    unstarted_class = (Class.BROWSER_DISCONNECTED if reason == "browser_disconnected"
                       else Class.CANCELLED)
    for index, record in enumerate(records):
        if record is None:
            records[index] = {"item": todo[index], "ok": False,
                              "class": unstarted_class.value,
                              "error": f"parallel item did not start: {reason}"}
    return [record for record in records if record is not None]


def summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts plus distinct failure classes and cleanup failures."""
    failed = [record for record in records if not record["ok"]]
    classes: dict[str, int] = {}
    cleanup_failures = []
    for record in records:
        if not record["ok"]:
            cls = record.get("class", "Error")
            classes[cls] = classes.get(cls, 0) + 1
        cleanup_failures.extend(record.get("cleanup_failures") or [])
    return {"total": len(records), "ok": len(records) - len(failed), "failed": len(failed),
            "classes": classes, "cleanup_failures": cleanup_failures,
            "values": [record["value"] for record in records if record["ok"]]}
