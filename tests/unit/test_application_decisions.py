import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[1] / "bench" / "application_decisions.py"
SPEC = importlib.util.spec_from_file_location("application_decisions", MODULE_PATH)
application_decisions = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(application_decisions)


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")


def test_measure_separates_shared_decisions_from_application_outcomes(tmp_path):
    root = tmp_path / "run-marker"
    attempts = root / "logs" / "attempts.jsonl"
    base = {
        "started": 100.0, "finished": 110.0, "submitted": False,
        "errors": [], "filled_count": 4,
    }
    _write_jsonl(attempts, [
        {**base, "attempt": "one", "required_unfilled": [
            {"status": "missing", "reason": "not stated in the CV"}]},
        {**base, "attempt": "one", "started": 111.0, "finished": 112.0,
         "required_unfilled": []},
        {**base, "attempt": "two", "required_unfilled": [
            {"label": "Phone\n+41", "status": "failed",
             "reason": "value was rejected"}]},
    ])
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"results": [{"attempt": "one"}, {"attempt": "two"}]}))
    _write_jsonl(root / "recordings" / "one" / "session.jsonl", [
        {"kind": "invoke", "ts": 110, "ms_total": 10, "source_lines": 20},
        {"kind": "call", "outcome": {"ok": False, "retryable": True}},
    ])
    _write_jsonl(root / "recordings" / "two" / "session.jsonl", [
        {"kind": "invoke", "ts": 110, "ms_total": 10, "source_lines": 20},
    ])

    result = application_decisions.measure(attempts, manifest=manifest)

    assert result["run"]["retries"] == 1
    assert result["selected"] == {
        "applications": 2,
        "dry_run_successes": 2,
        "autonomous_ready": 1,
        "needs_human_intervention": 0,
        "human_intervention_fields": 0,
        "has_technical_blockers": 1,
        "technical_blocker_fields": 1,
    }
    assert result["harness"]["invocations"] == 1
    assert result["harness"]["retryable_helper_failures"] == 1
    assert result["applications"][1]["technical_blockers"] == [{
        "label": "Phone +41", "status": "failed", "reason": "value was rejected"}]
    assert application_decisions.decision_pack(result) == {
        "next_action": "resolve_technical_blockers",
        "safety": {"submitted": 0},
        "outcomes": result["selected"],
        "retries": 1,
        "technical_blockers": [{
            "attempt": "two",
            "evidence": [{
                "label": "Phone +41", "status": "failed",
                "reason": "value was rejected",
            }],
        }],
    }


def test_codex_usage_is_paired_without_reading_message_content(tmp_path):
    root = tmp_path / "run-marker"
    attempts = root / "logs" / "attempts.jsonl"
    _write_jsonl(attempts, [{
        "attempt": "one", "started": 100.0, "finished": 110.0,
        "submitted": False, "errors": [], "filled_count": 4,
        "required_unfilled": [{"status": "skipped", "reason": "human choice required"}],
    }])
    transcript = tmp_path / "rollout.jsonl"

    def stamp(seconds):
        return datetime.fromtimestamp(seconds, UTC).isoformat()

    _write_jsonl(transcript, [
        {"timestamp": stamp(99), "type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "input": "run-marker"}},
        {"timestamp": stamp(101), "type": "event_msg", "payload": {
            "type": "token_count", "info": {"last_token_usage": {
                "input_tokens": 100, "cached_input_tokens": 80,
                "output_tokens": 10, "reasoning_output_tokens": 4}}}},
        {"timestamp": stamp(105), "type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "input": "tools.write_stdin"}},
        {"timestamp": stamp(106), "type": "event_msg", "payload": {
            "type": "token_count", "info": {"last_token_usage": {
                "input_tokens": 120, "cached_input_tokens": 100,
                "output_tokens": 12, "reasoning_output_tokens": 5}}}},
    ])

    result = application_decisions.measure(attempts, codex_transcript=transcript)

    assert result["model"]["invocations"] == 2
    assert result["model"]["polling_invocations"] == 1
    assert result["model"]["input_tokens"] == 220
    assert result["model"]["uncached_input_tokens"] == 40
    assert result["model"]["output_tokens"] == 22
    assert result["model"]["polling_tokens"] == {
        "input": 120, "cached_input": 100, "uncached_input": 20, "output": 12}
    assert result["selected"]["needs_human_intervention"] == 1


def test_pack_cli_emits_one_compact_json_line(tmp_path, monkeypatch, capsys):
    attempts = tmp_path / "run-marker" / "logs" / "attempts.jsonl"
    _write_jsonl(attempts, [{
        "attempt": "one", "started": 100.0, "finished": 110.0,
        "submitted": False, "errors": [], "filled_count": 4,
        "required_unfilled": [],
    }])
    monkeypatch.setattr(sys, "argv", ["application_decisions.py", str(attempts), "--pack"])

    assert application_decisions.main() == 0

    output = capsys.readouterr().out
    assert output.count("\n") == 1
    assert json.loads(output)["outcomes"]["applications"] == 1


def test_jsonl_rejects_malformed_records_with_line_number(tmp_path):
    path = tmp_path / "attempts.jsonl"
    path.write_text('{"attempt":"one"}\n\nnot-json\n', encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        application_decisions._jsonl(path)

    assert str(excinfo.value) == f"invalid JSON in {path}:3"


def test_measure_rejects_manifest_attempt_missing_from_log(tmp_path):
    attempts = tmp_path / "run-marker" / "logs" / "attempts.jsonl"
    _write_jsonl(attempts, [{
        "attempt": "one", "started": 100.0, "finished": 110.0,
        "submitted": False, "errors": [], "filled_count": 4,
        "required_unfilled": [],
    }])
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"results": [{"attempt": "one"}, {"attempt": "missing"}]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="manifest attempts missing from log"):
        application_decisions.measure(attempts, manifest=manifest)


def test_decision_pack_covers_every_next_action_branch():
    selected = {
        "applications": 1,
        "dry_run_successes": 1,
        "autonomous_ready": 1,
        "needs_human_intervention": 0,
        "human_intervention_fields": 0,
        "has_technical_blockers": 0,
        "technical_blocker_fields": 0,
    }
    cases = [
        ({"submitted": 1}, {"technical_blocker_fields": 1,
                            "human_intervention_fields": 1}, "stop"),
        ({"submitted": 0}, {"technical_blocker_fields": 1,
                            "human_intervention_fields": 1},
         "resolve_technical_blockers"),
        ({"submitted": 0}, {"technical_blocker_fields": 0,
                            "human_intervention_fields": 1}, "request_human_input"),
        ({"submitted": 0}, {"technical_blocker_fields": 0,
                            "human_intervention_fields": 0},
         "await_explicit_submission_authorization"),
    ]

    for run, overrides, expected in cases:
        pack = application_decisions.decision_pack({
            "run": {**run, "retries": 0},
            "selected": {**selected, **overrides},
            "applications": [],
        })
        assert pack["next_action"] == expected
