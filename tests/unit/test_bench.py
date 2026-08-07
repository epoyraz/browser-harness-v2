"""Bucket attribution: `harness` must mean cost we could optimise away."""
import json

from harness.core.bench import collapsible, rollup, steps


def _journal(tmp_path, calls, *, ms_total, ms_connect=100.0, ts=1_786_000_000.0):
    """One step: its helper spans, then the invoke record that closes it."""
    lines = [json.dumps({"kind": "call", "ts": ts, "fn": fn, "ms": ms, "cdp": 1})
             for fn, ms in calls]
    lines.append(json.dumps({"kind": "invoke", "ts": ts, "ok": True,
                             "ms_total": ms_total, "ms_connect": ms_connect,
                             "outcome": {"ok": True}}))
    p = tmp_path / "j.jsonl"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def test_blocking_helpers_are_wait_not_harness(tmp_path):
    """The regression: a collapsed joblens run reported 20.6s of `harness` that was
    joblens computing CV matches while wait_for sat idle. That reads as the harness being
    slow when it was doing nothing at all."""
    p = _journal(tmp_path, [("goto", 3000.0), ("wait_for", 15000.0), ("js", 400.0)],
                 ms_total=18_600.0)
    [step] = steps([p])
    assert step["ms_harness"] == 400.0          # only the js
    assert step["ms_blocked"] == 18_000.0       # goto + wait_for
    assert step["ms_wait"] == 18_100.0          # blocked + the 100ms outside any span


def test_the_same_wait_buckets_the_same_way_however_it_is_spelled(tmp_path):
    """`wait_for(...)` is inside a span and `time.sleep(...)` is not. Before this they
    landed in different buckets, so the comparison between two ways of writing one script
    was measuring the spelling."""
    for name in ("a", "b"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    explicit = _journal(tmp_path / "a", [("goto", 500.0), ("wait_for", 6000.0)],
                        ms_total=6600.0)
    slept = _journal(tmp_path / "b", [("goto", 500.0)], ms_total=6600.0)

    a, b = rollup([explicit]), rollup([slept])
    assert a["buckets"]["wait"] == b["buckets"]["wait"] == 6500.0
    assert a["buckets"]["harness"] == b["buckets"]["harness"] == 0.0


def test_non_blocking_helpers_still_count_as_harness(tmp_path):
    p = _journal(tmp_path, [("snapshot", 300.0), ("fill_form", 700.0)], ms_total=1200.0)
    r = rollup([p])
    assert r["buckets"]["harness"] == 1000.0
    assert r["blocked_ms"] == 0.0


def test_buckets_still_sum_to_the_measured_total(tmp_path):
    """Whatever the split, nothing may be invented or lost: connect + harness + wait must
    reconstruct the wall clock the invoke actually recorded."""
    p = _journal(tmp_path, [("goto", 2000.0), ("js", 250.0)],
                 ms_total=5000.0, ms_connect=300.0)
    [step] = steps([p])
    assert step["ms_connect"] + step["ms_harness"] + step["ms_wait"] == step["ms_total"]


def test_a_wait_only_step_is_still_collapsible(tmp_path):
    """wait_for is read-only: it observes and never changes page state, so a run of them
    remains safe to merge. Re-bucketing its *time* must not change that."""
    assert collapsible([
        {"fns": ["wait_for"]}, {"fns": ["js"]}, {"fns": ["fill_form"]},
    ]) == [{"from": 1, "to": 2, "steps": 2}]


def test_a_directory_of_journals_rolls_up_together(tmp_path):
    for i, ms in enumerate((1000.0, 2000.0)):
        d = tmp_path / f"s{i}"
        d.mkdir()
        _journal(d, [("js", ms)], ms_total=ms + 100.0)
    r = rollup([tmp_path])
    assert r["steps"] == 2
    assert r["buckets"]["harness"] == 3000.0
