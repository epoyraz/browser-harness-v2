"""`parallel()` — N tabs from one script, and the isolation that makes it safe."""
import threading
import time

import pytest

from harness.core.journal import Journal
from harness.core.outcome import ElementGone
from harness.ops.parallel import parallel, summarise


class FakeTab:
    def __init__(self, target_id):
        self.target_id = target_id


class FakeSession:
    """Only what parallel() touches. The cursor is thread-local, exactly like Session."""

    def __init__(self):
        self.journal = Journal(None)
        self._local = threading.local()
        self._n = 0
        self._lock = threading.Lock()
        self.created: list[str] = []
        self.closed: list[str] = []
        self.contexts: list[str] = []
        self.closed_contexts: list[str] = []
        self.open_tabs: set[str] = set()
        self.peak_tabs = 0

    def new_tab(self, url="about:blank", *, context_id=None):
        with self._lock:
            self._n += 1
            tid = f"T{self._n}"
            self.created.append(tid)
            self.open_tabs.add(tid)
            self.peak_tabs = max(self.peak_tabs, len(self.open_tabs))
        self._local.current = tid
        return FakeTab(tid)

    def use_tab(self, target_id):
        self._local.current = target_id
        return FakeTab(target_id)

    def close_tab(self, target_id=None, *, wait=True):
        with self._lock:
            self.closed.append(target_id)
            self.open_tabs.discard(target_id)

    def new_context(self):
        with self._lock:
            context_id = f"C{len(self.contexts) + 1}"
            self.contexts.append(context_id)
            return context_id

    def close_context(self, context_id):
        with self._lock:
            self.closed_contexts.append(context_id)

    @property
    def current(self):
        return getattr(self._local, "current", None)


def test_results_come_back_in_input_order_not_completion_order():
    """Completion order would be the natural implementation and silently wrong: callers
    zip results against the input."""
    s = FakeSession()

    def fn(item):
        time.sleep(0.02 if item % 2 == 0 else 0.001)   # evens finish last
        return item * 10

    out = parallel(s, range(8), fn, workers=4)
    assert [r["item"] for r in out] == list(range(8))
    assert [r["value"] for r in out] == [i * 10 for i in range(8)]
    assert all(r["telemetry"]["completed_ms"] >= r["telemetry"]["queued_ms"] for r in out)


def test_parallel_emits_start_and_completion_events_with_safe_identity():
    s = FakeSession()
    events = []
    out = parallel(s, [{"job_id": "a"}, {"job_id": "b"}], lambda item: item["job_id"],
                   workers=2, events=events.append)
    assert [(e["state"], e["item_id"]) for e in events].count(("started", "a")) == 1
    assert [(e["state"], e["item_id"]) for e in events].count(("completed", "a")) == 1
    assert {r["telemetry"]["item_id"] for r in out} == {"a", "b"}


def test_one_failing_item_does_not_cancel_its_siblings():
    s = FakeSession()

    def fn(item):
        if item == 3:
            raise ValueError("page exploded")
        return item

    out = parallel(s, range(6), fn, workers=3)
    assert len(out) == 6
    assert [r["ok"] for r in out] == [True, True, True, False, True, True]
    assert out[3]["error"] == "page exploded"
    assert out[3]["class"] == "ValueError"


def test_a_typed_harness_failure_keeps_its_evidence_across_the_worker_boundary():
    """rule 2: never discard a cause you were handed. The outcome contract is only worth
    having if it survives the thread that produced it."""
    s = FakeSession()

    def fn(item):
        raise ElementGone("gone", ref="e7")

    out = parallel(s, ["a"], fn, workers=1)
    assert out[0]["ok"] is False
    assert out[0]["class"] == "element_gone"
    assert out[0]["outcome"]["observed"]["ref"] == "e7"


def test_each_worker_sees_its_own_current_tab():
    """The property that makes the bare helpers safe inside fn: worker A's goto() must
    never redirect worker B's next js()."""
    s = FakeSession()
    seen: dict[int, set] = {}
    barrier = threading.Barrier(4, timeout=5)

    def fn(item):
        barrier.wait()                # force all four to overlap
        time.sleep(0.01)
        seen.setdefault(threading.get_ident(), set()).add(s.current)
        return s.current

    out = parallel(s, range(4), fn, workers=4)
    tabs = [r["value"] for r in out]
    assert len(set(tabs)) == 4                       # four distinct tabs
    assert all(len(v) == 1 for v in seen.values())   # nobody's cursor moved under them


def test_tabs_are_reused_across_items_and_cleaned_up():
    s = FakeSession()
    parallel(s, range(20), lambda i: i, workers=4)
    # At most one tab per worker, and emphatically not one per item — that reuse is the
    # difference between 8 tabs and 100 on a real sweep. Not exactly 4: a pool spins
    # threads up lazily, so trivial work can finish on fewer than max_workers.
    assert 1 <= len(s.created) <= 4
    assert set(s.closed) == set(s.created)


def test_reuse_tabs_false_gives_each_item_a_clean_tab():
    s = FakeSession()
    parallel(s, range(5), lambda i: i, workers=2, reuse_tabs=False)
    assert len(s.created) == 5
    assert set(s.closed) == set(s.created)
    assert s.peak_tabs <= 2


def test_clean_tab_mode_never_accumulates_the_full_input():
    s = FakeSession()
    parallel(s, range(100), lambda i: (time.sleep(0.002), i)[1], workers=5,
             reuse_tabs=False)
    assert len(s.created) == 100
    assert s.peak_tabs <= 5


def test_isolated_workers_use_contexts_not_more_browser_instances():
    s = FakeSession()
    parallel(s, range(20), lambda i: i, workers=4, isolated=True)
    assert 1 <= len(s.contexts) <= 4
    assert set(s.closed_contexts) == set(s.contexts)


def test_cancellation_prevents_queued_items_from_starting():
    from harness.ops.parallel import CancelToken

    s = FakeSession()
    token = CancelToken()
    started = []

    def fn(item):
        started.append(item)
        token.cancel()
        time.sleep(0.01)
        return item

    out = parallel(s, range(20), fn, workers=1, token=token)
    assert started == [0]
    assert out[0]["ok"] is True
    assert all(record["class"] == "cancelled" for record in out[1:])


def test_cleanup_failure_is_not_silently_reported_as_success():
    s = FakeSession()

    def fail_close(target_id=None, *, wait=True):
        raise RuntimeError("cannot close")

    s.close_tab = fail_close
    out = parallel(s, [1], lambda item: item, workers=1)
    assert out[0]["ok"] is False
    assert out[0]["class"] == "resource_cleanup_failed"
    assert out[0]["cleanup_failures"][0]["identifier"] == "T1"


def test_empty_input_opens_no_tabs():
    s = FakeSession()
    assert parallel(s, [], lambda i: i) == []
    assert s.created == []


def test_workers_never_exceed_the_item_count():
    s = FakeSession()
    parallel(s, [1, 2], lambda i: i, workers=16)
    assert len(s.created) <= 2


def test_workers_are_capped_at_ten_tabs():
    s = FakeSession()
    barrier = threading.Barrier(10, timeout=5)

    def fn(item):
        barrier.wait()
        return item

    parallel(s, range(20), fn, workers=16)
    assert len(s.created) == 10


def test_summarise_counts_and_groups_failure_classes():
    s = FakeSession()

    def fn(item):
        if item < 2:
            raise ElementGone("gone", ref="x")
        if item < 3:
            raise ValueError("nope")
        return item

    got = summarise(parallel(s, range(5), fn, workers=2))
    assert got["total"] == 5 and got["ok"] == 2 and got["failed"] == 3
    assert got["classes"] == {"element_gone": 2, "ValueError": 1}
    assert got["values"] == [3, 4]


# --- the concurrency bugs this feature had to fix first ----------------------

def test_spans_nest_per_thread_not_per_process(tmp_path):
    """With a shared stack, thread A's inner call would be recorded as a child of thread
    B's outer call, and cdp() would bill round trips to whichever span happened to be
    globally innermost — the trace tree and the round-trip counts become fiction exactly
    when concurrency makes them hardest to check by eye."""
    j = Journal(tmp_path / "j.jsonl", session="s")
    start = threading.Barrier(2, timeout=5)

    def worker(name):
        with j.call(f"outer-{name}"):
            start.wait()
            time.sleep(0.02)
            with j.call(f"inner-{name}"):
                j.cdp("Runtime.evaluate")

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    entries = {e["fn"]: e for e in j.entries() if e.get("kind") == "call"}
    outer = {n: entries[f"outer-{n}"] for n in ("a", "b")}
    for n in ("a", "b"):
        inner = entries[f"inner-{n}"]
        assert inner["parent"] == outer[n]["id"]     # its OWN outer, not the other thread's
        assert inner["cdp"] == 1
        assert outer[n].get("parent") is None


def test_a_fresh_thread_starts_with_an_empty_span_stack(tmp_path):
    j = Journal(tmp_path / "j.jsonl", session="s")
    depths = []
    with j.call("main"):
        t = threading.Thread(target=lambda: depths.append(len(j._stack)))
        t.start()
        t.join()
    assert depths == [0]        # not 1 — it must not inherit the parent thread's stack


@pytest.mark.parametrize("workers", [1, 4])
def test_every_item_is_accounted_for(workers):
    """Rule 1 of the outcome contract, applied to a fan-out: no item may vanish."""
    s = FakeSession()
    out = parallel(s, range(25), lambda i: i, workers=workers)
    assert sorted(r["item"] for r in out) == list(range(25))
