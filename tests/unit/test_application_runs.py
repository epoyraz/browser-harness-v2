import json

from tools.analyze_application_runs import analyze


def _run(path, rows, *, workers=10, wall_ms=1000):
    path.write_text(json.dumps({"meta": {"workers_effective": workers, "wall_ms": wall_ms},
                                "records": [
        {"ok": True, "value": {"job_id": job_id, **value}}
        for job_id, value in rows.items()
    ]}))
    return path


def test_repeat_analysis_separates_deterministic_and_transient(tmp_path):
    one = _run(tmp_path / "one.json", {
        "a": {"is_application": True},
        "b": {"is_application": False, "workflow_terminal": "bot_wall"},
    })
    two = _run(tmp_path / "two.json", {
        "a": {"is_application": True},
        "b": {"is_application": False, "workflow_terminal": "no_form"},
    })
    result = analyze([one, two], {"a": "form", "b": "bot_wall"})
    assert result["deterministic"] == 1 and result["transient"] == 1
    assert result["ground_truth"] == {"labelled": 2, "matching": 1}


def test_concurrency_recommendation_requires_comparable_worker_counts(tmp_path):
    rows = {"a": {"is_application": True, "diagnostics": {"event_loop_delay_ms": 5}}}
    slow = _run(tmp_path / "six.json", rows, workers=6, wall_ms=2000)
    fast = _run(tmp_path / "ten.json", rows, workers=10, wall_ms=1000)
    result = analyze([slow, fast])
    assert result["concurrency"]["recommended_workers"] == 10


def test_concurrency_recommendation_refuses_a_single_configuration(tmp_path):
    one = _run(tmp_path / "one.json", {"a": {"is_application": True}})
    assert analyze([one])["concurrency"]["recommended_workers"] is None


def test_concurrency_recommendation_explains_renderer_pressure(tmp_path):
    rows = {"a": {"is_application": True, "diagnostics": {"event_loop_delay_ms": 300}}}
    six = _run(tmp_path / "six.json", rows, workers=6, wall_ms=2000)
    ten = _run(tmp_path / "ten.json", rows, workers=10, wall_ms=1000)
    concurrency = analyze([six, ten])["concurrency"]
    assert concurrency["recommended_workers"] is None
    assert "250ms event-loop limit" in concurrency["reason"]
