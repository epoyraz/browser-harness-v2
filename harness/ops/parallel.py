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


class CancelToken:
    """Shared cooperative cancellation with an optional whole-run deadline."""

    def __init__(self, *, timeout: float | None = None):
        self._event = threading.Event()
        self.deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set() or (
            self.deadline is not None and time.monotonic() >= self.deadline)


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


def parallel(session: Any, items: Iterable[Any], fn: Callable[[Any], Any], *,
             workers: int = 0, reuse_tabs: bool = True, isolated: bool = False,
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
                        tab = session.new_tab(context_id=context_id)
                        item_tab = tab.target_id
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
                finally:
                    failures = []
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
                    "target_id": worker_tab or item_tab,
                    "browser_context_id": worker_context if reuse_tabs else item_context,
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

    reason = "deadline" if cancel.deadline is not None and time.monotonic() >= cancel.deadline \
        else "cancelled"
    for index, record in enumerate(records):
        if record is None:
            records[index] = {"item": todo[index], "ok": False,
                              "class": Class.CANCELLED.value,
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
