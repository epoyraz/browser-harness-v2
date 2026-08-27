"""Dry-run a direct-link job corpus through browser-harness.

Run through the harness namespace, not as ordinary Python:

    BH_JOURNAL=outputs/job-form-telemetry-2026-08-08/journal.jsonl \
      uv run bh < tools/collect_job_form_telemetry.py

The harness blocks submission. This script fills only facts supported by the CV and records
missing information rather than inventing it. CV upload is opt-in via
``BH_APPLICATION_UPLOADS=1`` because choosing a local file can transfer it to an employer
before a form is submitted. Results are crash-safe JSONL plus one input-ordered JSON
document.
"""
# The harness injects these helpers into the script namespace at runtime. Per-job broad
# catches are deliberate: one hostile page must become a typed record, not erase its peers.
# ruff: noqa: F821, BLE001
from __future__ import annotations

import csv
import ctypes
import hashlib
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from collections import defaultdict
from ctypes import wintypes
from pathlib import Path
from typing import Any

from applications import ontology, run_application
from applications.ontology import (
    norm,
    plan_for,
)
from harness.ops.forms import application_route_candidates
from harness.ops.profile import ApplicantProfile, load_answer_file

ROOT = Path.cwd()
INPUT = Path(os.environ.get("BH_APPLICATION_INPUT", ROOT / "jobs.json"))
OUT = Path(os.environ.get(
    "BH_APPLICATION_TELEMETRY_OUT",
    ROOT / "outputs" / "job-form-telemetry-2026-08-08",
))
CV = Path(os.environ.get(
    "BH_APPLICATION_CV",
    "/Users/rebourne/Desktop/Dev/jobsuche-101/Bewerbung2026/"
    "Lebenslauf/Lebenslauf – Enes Poyraz.pdf",
))
WORKERS = int(os.environ.get("BH_APPLICATION_WORKERS", "10"))
WORKER_LIMIT = int(os.environ.get("BH_APPLICATION_WORKER_LIMIT", "10"))
RUN_TIMEOUT = float(os.environ.get("BH_APPLICATION_TIMEOUT_SECONDS", str(4 * 60 * 60)))
MEMORY_INTERVAL = float(os.environ.get("BH_CHROME_MEMORY_INTERVAL_SECONDS", "2"))
MAX_HOPS = 2
UPLOAD_CV = os.environ.get("BH_APPLICATION_UPLOADS", "").strip().lower() in {
    "1", "true", "yes",
}

#: Which planner fills `run_application`'s callback. The rule table is the default because
#: a benchmark wants reproducibility; `BH_PLANNER=model` swaps in the Codex-backed one so
#: the same corpus can be run with a model actually in the loop.
PLANNER_KIND = os.environ.get("BH_PLANNER", "rules").strip().lower()

PROFILE = {
    "first_name": "Enes",
    "last_name": "Poyraz",
    "full_name": "Enes Poyraz",
    "email": "epoyraz.eth@gmail.com",
    "phone": "+41 77 266 98 58",
    "street": "Büchelerstrasse 12",
    "street_name": "Büchelerstrasse",
    "house_number": "12",
    "postal_code": "8212",
    "city": "Neuhausen am Rheinfall",
    "country_en": "Switzerland",
    "country_de": "Schweiz",
    "country_fr": "Suisse",
    "nationality_en": "Swiss",
    "nationality_de": "Schweiz",
    "nationality_fr": "Suisse",
    "birth_date_iso": "1987-02-05",
    "birth_date_local": "05.02.1987",
    "birth_place": "Gelsenkirchen, Deutschland",
    "current_company": "Xona AI",
    "current_title": "Co-Founder & Full-Stack Software Engineer",
    "headline": "Senior Full-Stack Software Engineer & Co-Founder",
    "experience_years": "8+",
    "location": "Neuhausen am Rheinfall, Switzerland",
    "education": "M.Sc. Bioinformatik",
    "english": "Fluent",
    "german": "Native",
    "turkish": "Fluent",
    "summary": (
        "Senior Full-Stack Software Engineer with over 8 years of experience building "
        "and operating cloud-native web platforms with C#/.NET, TypeScript/Next.js and "
        "Microsoft Azure. Hands-on co-founder of an AI SaaS platform with strengths in "
        "identity and access management, Kubernetes, Terraform and CI/CD."
    ),
}

REQUIRED = ROOT / "required.txt"
APPLICANT = ApplicantProfile.from_mapping(PROFILE, source=str(CV))
if REQUIRED.is_file():
    APPLICANT = APPLICANT.merged(load_answer_file(REQUIRED))
# The ontology knows what fields ask for; this run supplies what to answer.
ontology.configure(APPLICANT, PROFILE, str(CV))











SEMANTIC_CACHE: dict[tuple[Any, ...], str] = {}






















_MODEL_PLANNER = None


def active_planner():
    """`plan_for`, or the model-backed planner when one is asked for.

    Imported lazily and by path: the model planner imports this module in turn, and a
    benchmark that does not use it should not pay for its import or require the CLI to
    exist.
    """
    global _MODEL_PLANNER
    if PLANNER_KIND != "model":
        return plan_for
    if _MODEL_PLANNER is None:
        import importlib.util
        # This module is read two ways: imported normally by the scorers, and `exec`d by
        # `bh`, which supplies no `__file__`. The first run with a model planner failed
        # 100/100 on that alone.
        here = (Path(globals()["__file__"]).resolve().parent
                if "__file__" in globals() else ROOT / "tools")
        spec = importlib.util.spec_from_file_location(
            "bh_model_planner", here / "model_planner.py")
        _MODEL_PLANNER = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_MODEL_PLANNER)
    return _MODEL_PLANNER.plan_for


def planner_stats() -> dict[str, Any]:
    if PLANNER_KIND != "model" or _MODEL_PLANNER is None:
        return {"scripted": True, "model_calls": 0, "input_tokens": 0, "output_tokens": 0}
    stats = _MODEL_PLANNER.stats()
    return {"scripted": False, "model": _MODEL_PLANNER.MODEL,
            "reasoning_effort": _MODEL_PLANNER.EFFORT,
            # A claim about the payload, not about what the child could reach. It is a
            # coding agent with filesystem access; it is now launched with no user config
            # and an empty working directory, which is mitigation, not isolation.
            "values_sent_to_model": False, "answer_keys_sent": True,
            "child_isolated": False, **stats}


def cv_inputs(file_inputs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not file_inputs:
        return [], []
    cv_words = ("cv", "resume", "résumé", "lebenslauf", "curriculum")
    scored = [f for f in file_inputs if any(w in norm(f"{f.get('name')} {f.get('label')}") for w in cv_words)]
    if scored:
        return scored, [f for f in file_inputs if f not in scored]
    if len(file_inputs) == 1:
        return file_inputs, []
    return [], file_inputs


def compact_error(error: Exception) -> dict[str, Any]:
    outcome = getattr(error, "outcome", None)
    return {
        "class": getattr(getattr(error, "cls", None), "value", type(error).__name__),
        "error": str(error)[:300],
        "outcome": outcome.to_json() if outcome is not None else None,
    }


class ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class RunActivity:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.started = 0
        self.completed = 0
        self.peak_active = 0

    def event(self, value: dict[str, Any]) -> None:
        with self.lock:
            if value.get("state") == "started":
                self.active += 1
                self.started += 1
                self.peak_active = max(self.peak_active, self.active)
            elif value.get("state") == "completed":
                self.active = max(0, self.active - 1)
                self.completed += 1

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return {
                "active_attempts": self.active,
                "started_attempts": self.started,
                "completed_attempts": self.completed,
                "peak_active_attempts": self.peak_active,
            }


class ChromeMemorySampler:
    """Sample the Chrome instance connected to this harness session.

    CDP identifies every process owned by this browser instance. Windows supplies each
    process's working set and private bytes. Per-tab OS RAM remains an incremental average:
    site isolation can give one tab several renderer processes and can share processes.
    """

    def __init__(self, output: Path, activity: RunActivity, interval: float) -> None:
        self.output = output
        self.activity = activity
        self.interval = max(0.5, interval)
        self.samples: list[dict[str, Any]] = []
        self.started = time.perf_counter()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.baseline: dict[str, Any] | None = None
        self.kernel32 = None
        self.psapi = None
        if os.name == "nt":
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.psapi = ctypes.WinDLL("psapi", use_last_error=True)
            self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.kernel32.OpenProcess.restype = wintypes.HANDLE
            self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            self.psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD,
            ]
            self.psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

    def _memory(self, pid: int) -> tuple[int, int] | None:
        if self.kernel32 is None or self.psapi is None:
            return self._posix_memory(pid)
        handle = self.kernel32.OpenProcess(0x1010, False, pid)
        if not handle:
            return None
        try:
            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            if not self.psapi.GetProcessMemoryInfo(
                    handle, ctypes.byref(counters), counters.cb):
                return None
            return int(counters.WorkingSetSize), int(counters.PrivateUsage)
        finally:
            self.kernel32.CloseHandle(handle)

    def _posix_memory(self, pid: int) -> tuple[int, int] | None:
        """Resident and virtual size from `ps`, so the sampler reports real numbers here.

        Every run on this machine measured 34 Chrome processes and exactly zero bytes,
        because the only implementation was the Windows one — and `summary()` still called
        that `available: true`, so the report read as "Chrome used 0 bytes" rather than
        "not measured". `ps` has no per-process private-bytes equivalent, so RSS stands in
        for both columns and the summary says which platform produced the numbers.
        """
        try:
            out = subprocess.run(["/bin/ps", "-o", "rss=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        value = out.stdout.strip()
        if not value.isdigit():
            return None
        resident = int(value) * 1024                          # ps reports kilobytes
        return resident, resident

    def _capture(self) -> dict[str, Any]:
        activity = self.activity.snapshot()
        process_rows = session.conn.request("SystemInfo.getProcessInfo").get("processInfo") or []
        by_type: dict[str, dict[str, int]] = defaultdict(
            lambda: {"processes": 0, "measured": 0, "working_set_bytes": 0,
                     "private_bytes": 0}
        )
        measured = 0
        working_set = 0
        private = 0
        browser_pid = None
        for process in process_rows:
            process_type = str(process.get("type") or "unknown")
            pid = int(process.get("id") or 0)
            if process_type == "browser":
                browser_pid = pid
            by_type[process_type]["processes"] += 1
            memory = self._memory(pid)
            if memory is None:
                continue
            measured += 1
            working, private_bytes = memory
            working_set += working
            private += private_bytes
            by_type[process_type]["measured"] += 1
            by_type[process_type]["working_set_bytes"] += working
            by_type[process_type]["private_bytes"] += private_bytes
        try:
            page_targets = len(session.targets())
        except Exception:
            page_targets = None
        sample = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "offset_ms": round((time.perf_counter() - self.started) * 1000, 1),
            "browser_pid": browser_pid,
            "chrome_processes": len(process_rows),
            "measured_processes": measured,
            "working_set_bytes": working_set,
            "private_bytes": private,
            "page_targets": page_targets,
            "by_process_type": dict(by_type),
            **activity,
        }
        if self.baseline is not None:
            baseline_pages = self.baseline.get("page_targets")
            new_pages = None if page_targets is None or baseline_pages is None else max(
                0, page_targets - baseline_pages)
            sample["new_page_targets"] = new_pages
            sample["working_set_delta_bytes"] = working_set - self.baseline["working_set_bytes"]
            sample["private_delta_bytes"] = private - self.baseline["private_bytes"]
            if new_pages:
                sample["working_set_delta_per_new_page_bytes"] = round(
                    sample["working_set_delta_bytes"] / new_pages)
                sample["private_delta_per_new_page_bytes"] = round(
                    sample["private_delta_bytes"] / new_pages)
        return sample

    def _append(self, sample: dict[str, Any]) -> None:
        self.samples.append(sample)
        with self.output.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(sample, ensure_ascii=False, default=str) + "\n")

    def start(self) -> None:
        self.output.write_bytes(b"")
        self.baseline = self._capture()
        self._append(self.baseline)

        def loop() -> None:
            while not self.stop_event.wait(self.interval):
                try:
                    self._append(self._capture())
                except Exception as error:
                    self._append({
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "offset_ms": round((time.perf_counter() - self.started) * 1000, 1),
                        "error": compact_error(error),
                        **self.activity.snapshot(),
                    })

        self.thread = threading.Thread(target=loop, name="chrome-memory", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=self.interval + 5)
        try:
            self._append(self._capture())
        except Exception as error:
            self._append({
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "offset_ms": round((time.perf_counter() - self.started) * 1000, 1),
                "final_capture_error": compact_error(error),
                **self.activity.snapshot(),
            })

    def summary(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        # "Structurally valid" was the old test, and it passed on a run that measured
        # nothing: every sample carried `working_set_bytes: 0` and the report announced
        # `available: true`. A sample that measured no process is evidence of a missing
        # implementation, not of a browser using no memory.
        valid = [sample for sample in self.samples
                 if sample.get("measured_processes")]
        if not valid or self.baseline is None:
            return {"available": False, "samples": len(self.samples),
                    "reason": "no Chrome process could be measured on this platform",
                    "platform": sys.platform}
        expected_workers = min(WORKERS, WORKER_LIMIT)
        steady = [sample for sample in valid
                  if sample.get("active_attempts") == expected_workers
                  and (sample.get("new_page_targets") or 0) >= expected_workers]
        per_page_private = [sample["private_delta_per_new_page_bytes"] for sample in steady
                            if sample.get("new_page_targets") and
                            sample.get("private_delta_per_new_page_bytes", 0) >= 0]
        per_page_working = [sample["working_set_delta_per_new_page_bytes"] for sample in steady
                            if sample.get("new_page_targets") and
                            sample.get("working_set_delta_per_new_page_bytes", 0) >= 0]
        per_worker_private = [sample.get("private_delta_bytes", 0) / expected_workers
                              for sample in steady if sample.get("private_delta_bytes", 0) >= 0]
        per_worker_working = [sample.get("working_set_delta_bytes", 0) / expected_workers
                              for sample in steady if sample.get("working_set_delta_bytes", 0) >= 0]
        heaps = []
        for record in records:
            metrics = (((record.get("value") or {}).get("diagnostics") or {}).get("metrics") or {})
            if metrics.get("JSHeapUsedSize") is not None:
                heaps.append(float(metrics["JSHeapUsedSize"]))
        return {
            "available": True,
            "platform": sys.platform,
            "method": (
                "CDP SystemInfo process IDs plus Windows working set/private bytes; "
                "per-tab OS RAM is the Chrome delta divided by new page targets."
                if os.name == "nt" else
                "CDP SystemInfo process IDs plus `ps` resident set size; per-tab OS RAM "
                "is the Chrome delta divided by new page targets. RSS stands in for both "
                "the working-set and private columns — `ps` exposes no private-bytes "
                "equivalent, so the two are equal here by construction, not by measurement."
            ),
            "caveat": (
                "Incremental per-tab RAM is an average, not exact attribution: Chrome can "
                "share processes and create multiple renderer processes for one tab."
            ),
            "samples": len(valid),
            "interval_seconds": self.interval,
            "baseline": self.baseline,
            "peak_working_set_bytes": max(sample["working_set_bytes"] for sample in valid),
            "peak_private_bytes": max(sample["private_bytes"] for sample in valid),
            "peak_working_set_delta_bytes": max(
                sample.get("working_set_delta_bytes", 0) for sample in valid),
            "peak_private_delta_bytes": max(sample.get("private_delta_bytes", 0) for sample in valid),
            "peak_page_targets": max(
                (sample.get("page_targets") or 0) for sample in valid),
            "peak_new_page_targets": max(
                (sample.get("new_page_targets") or 0) for sample in valid),
            "peak_active_attempts": self.activity.snapshot()["peak_active_attempts"],
            "steady_full_pool_samples": len(steady),
            "incremental_private_per_new_page_bytes": numeric_summary(per_page_private),
            "incremental_working_set_per_new_page_bytes": numeric_summary(per_page_working),
            "incremental_private_per_worker_tab_bytes": numeric_summary(per_worker_private),
            "incremental_working_set_per_worker_tab_bytes": numeric_summary(per_worker_working),
            "per_attempt_js_heap_used_bytes": numeric_summary(heaps),
        }


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def numeric_summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min": round(min(values), 1),
        "mean": round(statistics.fmean(values), 1),
        "median": round(statistics.median(values), 1),
        "p95": round(percentile(values, 0.95) or 0, 1),
        "max": round(max(values), 1),
    }


def write_timing_reports(records: list[dict[str, Any]], whole_wall_ms: float) -> dict[str, Any]:
    attempts = []
    by_company: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        item = record.get("item") or {}
        value = record.get("value") or {}
        telemetry = record.get("telemetry") or {}
        metrics = ((value.get("diagnostics") or {}).get("metrics") or {})
        attempt = {
            "rank": item.get("rank"),
            "company": item.get("company"),
            "company_aliases": item.get("company_aliases") or [item.get("company")],
            "employer_group_id": item.get("employer_group_id"),
            "employer_site": item.get("employer_site"),
            "job_id": item.get("job_id"),
            "title": item.get("title"),
            "ok": bool(record.get("ok")),
            "status": value.get("status") or record.get("class"),
            "duration_ms": telemetry.get("duration_ms"),
            "work_wall_ms": value.get("wall_ms"),
            "queued_ms": telemetry.get("queued_ms"),
            "completed_ms": telemetry.get("completed_ms"),
            "worker_id": telemetry.get("worker_id"),
            "target_id": telemetry.get("target_id"),
            "cleanup_target_query_ms": telemetry.get("cleanup_target_query_ms"),
            "cleanup_descendants": telemetry.get("cleanup_descendants"),
            "js_heap_used_bytes": metrics.get("JSHeapUsedSize"),
        }
        attempts.append(attempt)
        for alias in attempt["company_aliases"]:
            by_company[str(alias or "")].append(attempt)

    company_rows = []
    for company, company_attempts in sorted(by_company.items(), key=lambda item: item[0].casefold()):
        durations = [float(row["duration_ms"]) for row in company_attempts
                     if row.get("duration_ms") is not None]
        starts = [float(row["queued_ms"]) for row in company_attempts
                  if row.get("queued_ms") is not None]
        ends = [float(row["completed_ms"]) for row in company_attempts
                if row.get("completed_ms") is not None]
        company_rows.append({
            "company": company,
            "attempts": len(company_attempts),
            "successful_worker_attempts": sum(bool(row["ok"]) for row in company_attempts),
            "statuses": dict(sorted({
                status: sum(row["status"] == status for row in company_attempts)
                for status in {str(row["status"]) for row in company_attempts}
            }.items())),
            "attempt_duration_ms": numeric_summary(durations),
            "sum_attempt_duration_ms": round(sum(durations), 1),
            "observed_span_ms": round(max(ends) - min(starts), 1) if starts and ends else None,
        })

    report = {
        "whole_set": {
            "attempts": len(attempts),
            "companies": len(company_rows),
            "wall_ms": whole_wall_ms,
            "sum_attempt_duration_ms": round(sum(
                float(row["duration_ms"]) for row in attempts if row.get("duration_ms") is not None
            ), 1),
            "attempt_duration_ms": numeric_summary([
                float(row["duration_ms"]) for row in attempts if row.get("duration_ms") is not None
            ]),
            "cleanup_target_query_ms": numeric_summary([
                float(row["cleanup_target_query_ms"])
                for row in attempts if row.get("cleanup_target_query_ms") is not None
            ]),
            "sum_cleanup_target_query_ms": round(sum(
                float(row["cleanup_target_query_ms"])
                for row in attempts if row.get("cleanup_target_query_ms") is not None
            ), 1),
            "cleanup_descendants": sum(
                int(row.get("cleanup_descendants") or 0) for row in attempts),
        },
        "companies": company_rows,
        "attempts": attempts,
    }
    (OUT / "timing-report.json").write_bytes(
        (json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
    fields = list(attempts[0]) if attempts else []
    with (OUT / "attempt-timing.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(attempts)
    company_fields = [
        "company", "attempts", "successful_worker_attempts", "sum_attempt_duration_ms",
        "observed_span_ms", "mean_attempt_ms", "median_attempt_ms", "p95_attempt_ms",
        "max_attempt_ms", "statuses",
    ]
    with (OUT / "company-timing.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=company_fields)
        writer.writeheader()
        for row in company_rows:
            duration = row["attempt_duration_ms"]
            writer.writerow({
                "company": row["company"],
                "attempts": row["attempts"],
                "successful_worker_attempts": row["successful_worker_attempts"],
                "sum_attempt_duration_ms": row["sum_attempt_duration_ms"],
                "observed_span_ms": row["observed_span_ms"],
                "mean_attempt_ms": duration.get("mean"),
                "median_attempt_ms": duration.get("median"),
                "p95_attempt_ms": duration.get("p95"),
                "max_attempt_ms": duration.get("max"),
                "statuses": json.dumps(row["statuses"], ensure_ascii=False),
            })
    return report


def add_diagnostics(result: dict[str, Any]) -> None:
    try:
        with session.journal.bind(stage="diagnostics"):
            result["diagnostics"] = session.tab().diagnostics()
    except Exception as error:
        result["diagnostics"] = {"error": compact_error(error)}


def one_job(job: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    apply = job.get("apply") or {}
    start_url = apply.get("direct_url") or job.get("url")
    result: dict[str, Any] = {
        "rank": job.get("rank"), "job_id": job.get("job_id"),
        "company": job.get("company"), "title": job.get("title"),
        "ats": apply.get("ats"), "declared_mode": apply.get("mode"),
        "start_url": start_url, "hops": [], "errors": [],
    }
    with session.journal.bind(stage="diagnostics"):
        result["diagnostics_started"] = session.tab().start_diagnostics()
    try:
        application = run_application(
            session,
            start_url, timeout=25, transition_timeout=15, hop_budget=6,
            candidates=application_route_candidates(start_url),
            planner=active_planner())
    except Exception as error:
        error_class = getattr(getattr(error, "cls", None), "value", type(error).__name__)
        result["status"] = ("navigation_failed" if error_class == "navigation_failed"
                            else "workflow_failed")
        result["errors"].append(compact_error(error))
        add_diagnostics(result)
        result["wall_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return result

    workflow = application["location"]
    prepared = workflow["prepared"]
    skill_packet = application.get("skills") or {}
    result["skills"] = {
        "enabled": bool(skill_packet.get("enabled")),
        "matches": skill_packet.get("matches") or [],
        "bytes": int(skill_packet.get("bytes") or 0),
        "sha256": skill_packet.get("sha256"),
    }
    result["navigation"] = workflow["navigation"]
    result["navigate_ms"] = workflow["wall_ms"]
    result["workflow_terminal"] = workflow["terminal_state"]
    result["hops"] = workflow["hops"]

    schema = prepared.get("schema") or {}
    result["landed_url"] = prepared.get("url") or result.get("navigation", {}).get("landed")
    result["language"] = prepared.get("language")
    result["context"] = prepared.get("context")
    result["contexts_checked"] = prepared.get("contexts_checked")
    result["schema"] = schema
    verdict = schema.get("verdict") or {}
    result["form_classification"] = verdict.get("classification") or "unknown"
    result["is_authentication"] = bool(verdict.get("is_authentication"))
    result["is_generic_form"] = bool(verdict.get("is_generic_form"))
    result["is_application"] = bool(prepared.get("is_application"))
    if not result["is_application"]:
        if result["is_authentication"]:
            result["status"] = "authentication_required"
        elif result["is_generic_form"]:
            result["status"] = "generic_form"
        else:
            result["status"] = "no_application_form"
        add_diagnostics(result)
        result["wall_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return result

    plan, audit = application["plan"], application["audit"]
    result["field_audit"] = audit
    result["fill_plan_count"] = len(plan)
    result["fill_ms"] = application.get("fill_ms")
    result["fill"] = application.get("fill")

    file_inputs = prepared.get("file_inputs") or []
    chosen, ambiguous = cv_inputs(file_inputs)
    result["file_inputs"] = file_inputs
    result["ambiguous_file_inputs"] = ambiguous
    uploads = []
    if UPLOAD_CV:
        for item in chosen:
            try:
                t0 = time.perf_counter()
                outcome = upload_file(item["ref"], str(CV), timeout=25)
                uploads.append({"input": item,
                                "ms": round((time.perf_counter() - t0) * 1000, 1),
                                "outcome": outcome.to_json()})
            except Exception as error:
                uploads.append({"input": item, "error": compact_error(error)})
    result["uploads"] = uploads
    result["upload_skipped"] = bool(chosen) and not UPLOAD_CV
    result["status"] = "form_processed"
    add_diagnostics(result)
    result["wall_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


def main() -> None:
    if not INPUT.is_file() or (UPLOAD_CV and not CV.is_file()):
        raise RuntimeError(f"missing input: jobs={INPUT.is_file()} cv={CV.is_file()}")
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = json.loads(INPUT.read_text(encoding="utf-8"))["jobs"]
    if not jobs:
        raise RuntimeError("the input contains no jobs")
    completed_path = OUT / "results-completion-order.jsonl"
    completed_path.write_bytes(b"")
    write_lock = threading.Lock()
    run_started = time.time()
    activity = RunActivity()
    memory = ChromeMemorySampler(OUT / "chrome-memory-samples.jsonl", activity, MEMORY_INTERVAL)
    memory.start()

    completion_sequence = 0

    def progress(done: int, total: int, record: dict[str, Any]) -> None:
        nonlocal completion_sequence
        with write_lock:
            completion_sequence += 1
            with completed_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"sequence": completion_sequence, **record},
                                    ensure_ascii=False, default=str) + "\n")
        if done == 1 or done % 25 == 0 or done == total:
            value = record.get("value") or {}
            print(f"progress {done}/{total} rank={value.get('rank')} status={value.get('status')}", flush=True)

    try:
        records = parallel(
            jobs, one_job, workers=WORKERS, worker_limit=WORKER_LIMIT,
            reuse_tabs=True, isolated=False, timeout=RUN_TIMEOUT, progress=progress,
            events=activity.event, item_id=lambda job: job["job_id"],
        )
    finally:
        memory.stop()
    whole_wall_ms = round((time.time() - run_started) * 1000, 1)
    summary = summarise(records)
    summary.pop("values", None)  # records below are authoritative; do not duplicate them.
    memory_summary = memory.summary(records)
    timing_report = write_timing_reports(records, whole_wall_ms)
    (OUT / "chrome-memory-summary.json").write_bytes(
        (json.dumps(memory_summary, indent=2, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
    payload = {
        "meta": {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "input": str(INPUT), "cv": str(CV), "workers_requested": WORKERS,
            "worker_limit": WORKER_LIMIT,
            "workers_effective": min(WORKERS, WORKER_LIMIT, len(jobs)), "dry_run": True,
            "submissions": 0, "cv_uploads_enabled": UPLOAD_CV,
            "wall_ms": whole_wall_ms,
            "timeout_seconds": RUN_TIMEOUT,
            "profile_sources": sorted({item.source for item in APPLICANT.values.values()}),
            "model_boundary": {
                **planner_stats(),
                "planner": PLANNER_KIND,
                "decision_packet_fields": sorted(APPLICANT.values),
                "application_skills": os.environ.get("BH_APPLICATION_SKILLS", "1"),
                "skill_context_delivered_to_planner": True,
                "skill_context_interpreted": False,
                "decision_packet_sha256": hashlib.sha256("\n".join(
                    sorted(APPLICANT.values)).encode()).hexdigest()[:16],
            },
            "semantic_cache": {"strategies": len(SEMANTIC_CACHE),
                               "hits": SEMANTIC_CACHE_HITS, "stores_live_refs": False},
        },
        "parallel_summary": summary,
        "memory_summary": memory_summary,
        "timing_summary": timing_report["whole_set"],
        "artifacts": {
            "completion_order": str(completed_path),
            "memory_samples": str(OUT / "chrome-memory-samples.jsonl"),
            "memory_summary": str(OUT / "chrome-memory-summary.json"),
            "timing_report": str(OUT / "timing-report.json"),
            "attempt_timing_csv": str(OUT / "attempt-timing.csv"),
            "company_timing_csv": str(OUT / "company-timing.csv"),
        },
        "records": records,
    }
    (OUT / "results.json").write_bytes(
        (json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
    print(json.dumps({"output": str(OUT / "results.json"),
                      "summary": payload["parallel_summary"],
                      "wall_ms": payload["meta"]["wall_ms"]}, default=str))


if __name__ in {"__main__", "__bh__"}:
    main()
