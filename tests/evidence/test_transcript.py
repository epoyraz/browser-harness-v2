"""Think time read from the agent's own transcript — the one bucket the harness cannot see."""
import json

from evidence.bench import rollup
from evidence.transcript import attach, gaps, summarise


def _line(kind: str, ts: str, **kw) -> str:
    """One transcript entry. Mirrors Claude Code's shape: tool_use/tool_result blocks live
    inside message.content, and a plain string content marks a human turn."""
    if kind == "prompt":
        e = {"type": "user", "timestamp": ts, "message": {"content": "do the thing"}}
    elif kind == "use":
        e = {"type": "assistant", "timestamp": ts,
             "message": {"content": [{"type": "tool_use", "name": "Bash",
                                      "input": {"command": kw.get("cmd", "bh")}}],
                         "model": "claude-opus-5", "usage": {"output_tokens": 100}}}
    else:
        e = {"type": "user", "timestamp": ts,
             "message": {"content": [{"type": "tool_result", "tool_use_id": "x"}]}}
    if kw.get("sidechain"):
        e["isSidechain"] = True
    return json.dumps(e)


def _write(tmp_path, lines):
    p = tmp_path / "session.jsonl"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def test_think_is_the_gap_from_the_previous_result_to_the_next_call(tmp_path):
    p = _write(tmp_path, [
        _line("prompt", "2026-08-07T10:00:00Z"),
        _line("use", "2026-08-07T10:00:02Z"),      # after a prompt — excluded
        _line("res", "2026-08-07T10:00:05Z"),
        _line("use", "2026-08-07T10:00:20Z"),      # 15s of thinking
        _line("res", "2026-08-07T10:00:22Z"),
    ])
    rows = gaps(p)
    assert [r["after_prompt"] for r in rows] == [True, False]
    assert rows[1]["seconds"] == 15.0
    assert summarise(rows)["n"] == 1
    assert summarise(rows)["p50_s"] == 15.0


def test_a_gap_spanning_a_human_turn_is_not_thinking(tmp_path):
    """The bug this exists for: measured over a real session, the largest 'think time' was
    16.9 hours — an overnight break between a tool result and the next morning's reply."""
    p = _write(tmp_path, [
        _line("prompt", "2026-08-07T10:00:00Z"),
        _line("use", "2026-08-07T10:00:02Z"),      # opens the turn — excluded
        _line("res", "2026-08-07T10:00:03Z"),
        _line("use", "2026-08-07T10:00:15Z"),      # 12s — a real decision, kept
        _line("res", "2026-08-07T10:00:16Z"),
        _line("prompt", "2026-08-08T09:00:00Z"),   # human slept
        _line("use", "2026-08-08T09:00:04Z"),      # ~17h since the last result — excluded
    ])
    rows = gaps(p)
    assert rows[-1]["after_prompt"] is True
    s = summarise(rows)
    assert s["excluded_after_prompt"] == 2
    assert s["n"] == 1                 # the real one survives...
    assert s["max_s"] == 12.0          # ...and the overnight gap is nowhere in the stats


def test_idle_outliers_are_dropped_but_counted(tmp_path):
    p = _write(tmp_path, [
        _line("use", "2026-08-07T10:00:00Z"), _line("res", "2026-08-07T10:00:01Z"),
        _line("use", "2026-08-07T10:00:11Z"), _line("res", "2026-08-07T10:00:12Z"),
        _line("use", "2026-08-07T11:00:00Z"),
    ])
    s = summarise(gaps(p), idle_s=300.0)
    assert s["n"] == 1 and s["excluded_idle"] == 1     # never silently truncated


def test_subagent_turns_do_not_double_bill_wall_clock(tmp_path):
    p = _write(tmp_path, [
        _line("use", "2026-08-07T10:00:00Z"), _line("res", "2026-08-07T10:00:01Z"),
        _line("use", "2026-08-07T10:00:03Z", sidechain=True),
        _line("use", "2026-08-07T10:00:09Z"),
    ])
    assert len(gaps(p)) == 2
    assert len(gaps(p, include_sidechain=True)) == 3


def test_attach_matches_steps_to_their_own_gap_and_feeds_rollup(tmp_path):
    """A journal step records when it *ended*; the transcript records when the call was
    issued. Alignment is on start time, one-to-one, within a spawn's tolerance."""
    journal = tmp_path / "j.jsonl"
    journal.write_text("\n".join(
        json.dumps({"kind": "invoke", "ts": ts, "ok": True, "ms_total": 2000.0,
                    "ms_connect": 100.0, "outcome": {"ok": True}})
        for ts in (1786096810.0, 1786096830.0)), encoding="utf-8")

    p = _write(tmp_path, [
        _line("prompt", "2026-08-07T09:59:50Z"),
        _line("use", "2026-08-07T09:59:52Z"),   # opens the turn — not a decision gap
        _line("res", "2026-08-07T09:59:58Z"),
        _line("use", "2026-08-07T10:00:08Z"),   # 10s think, step 1 starts 10:00:08
        _line("res", "2026-08-07T10:00:10Z"),
        _line("use", "2026-08-07T10:00:28Z"),   # 18s think, step 2 starts 10:00:28
    ])
    steps = rollup([journal])["step_list"]
    per = attach(steps, gaps(p))
    assert per == [10000.0, 18000.0]

    r = rollup([journal], think_by_step=per)
    assert r["think_source"] == "transcript"
    assert r["think_matched"] == 2
    assert r["buckets"]["think"] == 28000.0      # summed, not averaged-and-multiplied


def test_unmatched_steps_are_reported_not_guessed(tmp_path):
    journal = tmp_path / "j.jsonl"
    journal.write_text(json.dumps(
        {"kind": "invoke", "ts": 1786096810.0, "ok": True, "ms_total": 2000.0,
         "ms_connect": 100.0, "outcome": {"ok": True}}), encoding="utf-8")
    p = _write(tmp_path, [                       # a transcript from a different hour
        _line("res", "2026-08-07T02:00:00Z"), _line("use", "2026-08-07T02:00:10Z"),
    ])
    steps = rollup([journal])["step_list"]
    assert attach(steps, gaps(p)) == [None]
    r = rollup([journal], think_by_step=[None])
    assert r["think_matched"] == 0
    assert r["think_source"] == "inferred"       # falls back, and says so
