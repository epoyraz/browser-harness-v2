"""`parallel()` — one script, many tabs.

D0 says the lever is fewer *decisions*, and this is the other half of that argument.
Collapsing steps removes think time from a serial chain; parallelism removes wall clock
from the work that remains. Both attack step count: visiting 100 pages one per `bh` run is
100 decisions, visiting them in one `parallel()` call is one.

Measured, on the task this was built for: 100 job pages, two loads each, across 10 workers
took 131 seconds. Serially at the observed ~3s per load that is roughly 10 minutes, and as
one-page-per-invocation it would additionally have cost 200 model decisions at ~15s each.

**Why this is safe here and was not in v1.** v1's daemon held one shared `current_tab`, so
two subagents fought over one browser (#375). v2 keeps no cursor in the daemon — every
request names its target — and the client's cursor is now thread-local, so N workers can
hold N tabs with no way to steal each other's. The transport underneath was already
concurrent: `RemoteConnection` multiplexes over one websocket behind a lock with a
dedicated reader thread, which is why this needs no second connection per worker.

Failure policy follows D11 rule 2: a worker that raises does not cancel its siblings and
does not lose its cause. Every item gets a result record, in input order, and the caller
is told how many failed rather than having to compare list lengths.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from harness.core.outcome import HarnessError

#: Chrome renders every visible tab. Past roughly this many the tabs contend for the
#: compositor and each one gets slower, so more workers stop buying wall clock. Tunable
#: because a headless run with light pages can go wider.
DEFAULT_WORKERS = 8


def parallel(session: Any, items: Iterable[Any],
             fn: Callable[[Any], Any], *,
             workers: int = 0,
             reuse_tabs: bool = True) -> list[dict[str, Any]]:
    """Run `fn(item)` for each item, each in its own tab, and return one record per item.

    Each record is `{"item", "ok", "value"}` or `{"item", "ok": False, "error", "class"}`.
    Results come back in **input order**, not completion order — a caller almost always
    wants to zip them against the input, and completion order would make that silently
    wrong.

    Inside `fn`, the bare helpers (`goto`, `js`, `see`, …) address *this worker's* tab:
    the current-tab cursor is thread-local. Nothing needs to be threaded through.

    `reuse_tabs` keeps one tab per worker and reuses it across items, which is what makes
    a 100-item sweep cost 8 tabs rather than 100. Pass False when a page poisons its tab
    (a modal dialog, a permission prompt) and each item needs a clean one.
    """
    todo: Sequence[Any] = list(items)
    if not todo:
        return []
    n = workers or int(os.environ.get("BH_WORKERS") or 0) or DEFAULT_WORKERS
    n = max(1, min(n, len(todo)))

    # One tab per worker thread, created on that thread's first item and reused after.
    # Creating them up front would open tabs a short run never reaches.
    owned: dict[int, str] = {}
    import threading

    lock = threading.Lock()

    def run(index_item: tuple[int, Any]) -> tuple[int, dict[str, Any]]:
        i, item = index_item
        key = threading.get_ident()
        with lock:
            tid = owned.get(key)
        if tid is None or not reuse_tabs:
            tab = session.new_tab()
            with lock:
                owned[key] = tab.target_id
        else:
            session.use_tab(tid)          # rebind this thread's cursor to its own tab
        try:
            return i, {"item": item, "ok": True, "value": fn(item)}
        except HarnessError as e:
            # A typed harness failure keeps its evidence: the outcome contract is only
            # worth having if it survives a worker boundary.
            return i, {"item": item, "ok": False, "class": e.outcome.cls.value,
                       "error": str(e)[:200], "outcome": e.outcome.to_json()}
        except Exception as e:            # noqa: BLE001 — one bad page must not stop 99 good ones
            return i, {"item": item, "ok": False, "class": type(e).__name__,
                       "error": str(e)[:200]}

    out: list[dict[str, Any] | None] = [None] * len(todo)
    with ThreadPoolExecutor(max_workers=n, thread_name_prefix="bh-par") as pool:
        for i, record in pool.map(run, enumerate(todo)):
            out[i] = record

    # Tabs opened for the sweep are ours to clean up; a caller who wanted 100 tabs left
    # open would have opened them.
    for tid in set(owned.values()):
        try:
            session.close_tab(tid)
        except Exception:  # noqa: BLE001, S110 — teardown must not mask the results
            pass
    return [r for r in out if r is not None]


def summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts plus the distinct failure classes — enough to decide whether to retry.

    Separate from `parallel()` so the records stay the primitive: a caller that wants to
    inspect every outcome is not made to parse a summary string.
    """
    failed = [r for r in records if not r["ok"]]
    classes: dict[str, int] = {}
    for r in failed:
        classes[r.get("class", "Error")] = classes.get(r.get("class", "Error"), 0) + 1
    return {"total": len(records), "ok": len(records) - len(failed), "failed": len(failed),
            "classes": classes,
            "values": [r["value"] for r in records if r["ok"]]}
