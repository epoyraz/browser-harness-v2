"""Telemetry aggregates journals; it never opens a second capture path."""
import json

from harness.core.telemetry import KEEP, render, rollup


def _journal(tmp_path, rows):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def test_rollup_counts_calls_failures_and_round_trips(tmp_path):
    j = _journal(tmp_path, [
        {"kind": "call", "fn": "goto", "ms": 100, "cdp": 1, "outcome": {"ok": True}},
        {"kind": "call", "fn": "goto", "ms": 300, "cdp": 1, "outcome": {"ok": True}},
        {"kind": "call", "fn": "fill_form", "ms": 8, "cdp": 2,
         "outcome": {"ok": False, "class": "partial"}},
        {"kind": "note", "msg": "ignored"},
    ])
    r = rollup([j])
    assert r["calls"] == 3 and r["failed"] == 1
    goto = next(h for h in r["helpers"] if h["fn"] == "goto")
    assert goto["calls"] == 2 and goto["cdp_per_call"] == 1.0 and goto["failed"] == 0


def test_rollup_reports_recovered_protocol_failures_separately(tmp_path):
    j = _journal(tmp_path, [
        {"kind": "call", "fn": "click", "outcome": {"ok": True}},
        {"kind": "cdp", "method": "Page.handleJavaScriptDialog", "ok": False,
         "error_class": "cdp_error", "error_code": -32602},
        {"kind": "cdp", "method": "Runtime.evaluate", "ok": True},
    ])
    protocol = rollup([j])["protocol"]
    assert protocol["calls"] == 2 and protocol["failed"] == 1
    assert protocol["failure_codes"] == [("-32602", 1)]


def test_the_failure_histogram_is_the_prioritisation_signal(tmp_path):
    """v1 could not produce this: every failure was a `str`, so "which mode dominates"
    had no answer. With a closed enum it is a Counter."""
    j = _journal(tmp_path, [
        {"kind": "call", "fn": "goto", "outcome": {"ok": False, "class": "navigation_failed"}},
        {"kind": "call", "fn": "goto", "outcome": {"ok": False, "class": "navigation_failed"}},
        {"kind": "call", "fn": "fill_form", "outcome": {"ok": False, "class": "no_option_match"}},
    ])
    assert rollup([j])["failure_classes"] == [("navigation_failed", 2), ("no_option_match", 1)]


def test_no_url_arg_or_source_can_reach_the_rollup(tmp_path):
    """Privacy is structural: these fields are never selected, not redacted afterwards."""
    j = _journal(tmp_path, [{
        "kind": "call", "fn": "goto", "ms": 5, "cdp": 1,
        "args": {"url": "https://secret.example/?token=abc"},
        "url": "https://secret.example/", "outcome": {"ok": True}}])
    blob = json.dumps(rollup([j]))
    assert "secret.example" not in blob and "token" not in blob and "args" not in blob
    assert set(KEEP) == {"fn", "ms", "cdp"}


def test_a_truncated_final_line_costs_one_entry_not_the_file(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps({"kind": "call", "fn": "goto", "outcome": {"ok": True}})
                 + '\n{"kind": "call", "fn": "go')
    assert rollup([p])["calls"] == 1


def test_render_leads_with_failures(tmp_path):
    j = _journal(tmp_path, [
        {"kind": "call", "fn": "goto", "outcome": {"ok": False, "class": "timeout"}}])
    text = "\n".join(render(rollup([j])))
    assert "failure classes" in text and "timeout" in text


def test_an_empty_corpus_says_how_to_start_one(tmp_path):
    assert any("BH_JOURNAL" in line for line in render(rollup([tmp_path])))
