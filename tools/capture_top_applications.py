"""Capture the 30 best technically clean application forms without submitting.

Run through the harness so its dry-run boundary and optional recorder are active::

    BH_RECORD=1 BH_RECORDINGS=outputs/top-30-applications/recordings \
      uv run bh < tools/capture_top_applications.py

The selection comes from the latest 100-job telemetry: form reached, no harness errors,
and no failed field writes. File inputs are deliberately not touched because selecting a
file may upload it before submission. Values come from the collector's document-backed
profile; unsupported required controls stay empty and are counted in the manifest.
"""
# Harness helpers are injected into this script's globals.
# ruff: noqa: F821, BLE001
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from harness.ops.forms import application_route_candidates
from tools.collect_job_form_telemetry import MAX_HOPS, plan_for

ROOT = Path.cwd()
SOURCE = ROOT / "outputs" / "job-form-telemetry-2026-08-08" / "results.json"
OUT = ROOT / "outputs" / "top-30-applications-2026-08-08"
SHOTS = OUT / "screenshots"
LIMIT = 30


def safe_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "job").lower()).strip("-")[:60]


def select_jobs() -> list[dict[str, Any]]:
    payload = json.loads(SOURCE.read_text(encoding="utf-8"))
    selected = []
    for record in payload["records"]:
        if not record.get("ok"):
            continue
        value = record["value"]
        observed = ((value.get("fill") or {}).get("observed") or {})
        if (value.get("status") == "form_processed" and not value.get("errors")
                and int(observed.get("failed") or 0) == 0):
            selected.append(value)
    selected.sort(key=lambda item: int(item.get("rank") or 10_000))
    if len(selected) < LIMIT:
        raise RuntimeError(f"only {len(selected)} technically clean applications; need {LIMIT}")
    return selected[:LIMIT]


def compact_error(error: Exception) -> dict[str, str]:
    return {
        "class": getattr(getattr(error, "cls", None), "value", type(error).__name__),
        "message": str(error)[:240],
    }


def capture(job: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    rank = int(job["rank"])
    result: dict[str, Any] = {
        "rank": rank,
        "job_id": job.get("job_id"),
        "company": job.get("company"),
        "title": job.get("title"),
        "start_url": job.get("start_url"),
        "submitted": False,
        "cv_uploaded": False,
        "errors": [],
    }
    routes = application_route_candidates(str(job.get("start_url") or ""))
    try:
        goto(result["start_url"], timeout=25)
        prepared: dict[str, Any] = {}
        for hop in range(MAX_HOPS + 1):
            wait_for_application_state(timeout=10)
            prepared = session.prepare_application(timeout=18)
            if prepared.get("is_application") or hop >= MAX_HOPS:
                break
            if not (prepared.get("apply_control") or prepared.get("apply_link") or routes):
                break
            followed = session.follow_application(prepared, timeout=15, candidates=routes)
            if followed["transition"].get("kind") == "candidate_link":
                routes = []

        if not prepared.get("is_application"):
            result["errors"].append({"class": "not_a_form", "message": "form not reached"})
            return result

        schema = prepared.get("schema") or {}
        plan, audit = plan_for(schema, str(prepared.get("language") or "en"))
        result["fields_seen"] = len(audit)
        result["planned"] = len(plan)
        result["missing_required"] = sum(
            bool(field.get("required")) and field.get("status") == "missing_profile"
            for field in audit
        )
        result["unknown_required"] = sum(
            bool(field.get("required")) and field.get("status") == "unclassified"
            for field in audit
        )
        if plan:
            outcome = fill_form(plan, timeout=30)
            result["fill"] = outcome.to_json()
            if not outcome.ok:
                result["errors"].append({
                    "class": "partial",
                    "message": f"{outcome.observed.get('failed', 0)} field writes failed",
                })

        js("window.scrollTo(0, 0)")
        shot = SHOTS / f"{rank:03d}-{safe_name(job.get('company'))}.png"
        capture_screenshot(str(shot))
        result["screenshot"] = str(shot.relative_to(ROOT))
        result["landed_url"] = prepared.get("url")
    except Exception as error:
        result["errors"].append(compact_error(error))
    finally:
        result["wall_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    jobs = select_jobs()

    def progress(done: int, total: int, record: dict[str, Any]) -> None:
        value = record.get("value") or {}
        print(
            f"progress {done}/{total} rank={value.get('rank')} "
            f"errors={len(value.get('errors') or [])}",
            flush=True,
        )

    records = parallel(
        jobs,
        capture,
        workers=6,
        reuse_tabs=True,
        isolated=False,
        timeout=20 * 60,
        progress=progress,
    )
    values = [record.get("value") for record in records if record.get("ok")]
    manifest = {
        "meta": {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "selection": "top-ranked forms with no prior harness or field-write errors",
            "requested": LIMIT,
            "dry_run": True,
            "submissions": 0,
            "cv_uploads": 0,
        },
        "summary": {
            "worker_ok": sum(bool(record.get("ok")) for record in records),
            "screenshots": sum(bool(value and value.get("screenshot")) for value in values),
            "fresh_clean": sum(bool(value and not value.get("errors")) for value in values),
            "required_missing": sum(int((value or {}).get("missing_required") or 0) for value in values),
            "required_unknown": sum(int((value or {}).get("unknown_required") or 0) for value in values),
        },
        "records": records,
    }
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    print(json.dumps({"output": str(OUT), **manifest["summary"]}, ensure_ascii=False))


main()
