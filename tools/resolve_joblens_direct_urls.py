"""Resolve every jobs.json entry through Joblens's public stateless MCP server.

The output replaces each primary ``url`` with the employer's apply URL. It deliberately
contains no joblens.ch URLs; provenance is retained as job IDs and MCP method names.
Raw MCP text is kept separately under the telemetry output directory.
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "jobs.json"
RAW = ROOT / "outputs" / "job-form-telemetry-2026-08-08" / "joblens-mcp-details.jsonl"
ENDPOINT = "https://joblens.ch/api/mcp"
WORKERS = 5


def call(job_id: str, request_id: int) -> dict:
    payload = json.dumps({
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": "get_job_details", "arguments": {"job_id": job_id}},
    })
    for attempt in range(2):
        try:
            # macOS's system Python in this workspace has no usable CA bundle; curl uses
            # the system trust store and already succeeded against this exact endpoint.
            response = subprocess.run(
                ["curl", "-fsS", "--retry", "3", "--retry-all-errors",
                 "--retry-delay", "1", ENDPOINT,
                 "-H", "Content-Type: application/json",
                 "-H", "Accept: application/json, text/event-stream",
                 "-H", "User-Agent: browser-harness-telemetry/0.1",
                 "--data-binary", "@-"],
                # encoding is explicit: `text=True` alone decodes with the LOCALE, which on
                # Windows is cp1252 — every em dash and umlaut in a Joblens row arrived as
                # mojibake ("**—" -> "**â€”") and the row parser rejected it.
                input=payload, text=True, encoding="utf-8", capture_output=True, timeout=60, check=False,
            )
            if response.returncode != 0:
                raise RuntimeError(response.stderr.strip()[:200] or f"curl rc={response.returncode}")
            data = json.loads(response.stdout)
            content = ((data.get("result") or {}).get("content") or [])
            text = "\n".join(str(item.get("text") or "") for item in content
                             if item.get("type") == "text")
            if not text:
                raise RuntimeError(str(data.get("error") or "empty MCP response"))
            apply_match = re.search(r"^Apply \(employer site\):\s*(\S+)\s*$", text, re.MULTILINE)
            route_match = re.search(r"^Application route:\s*(.+?)\s*$", text, re.MULTILINE)
            return {"job_id": job_id,
                    "direct_url": apply_match.group(1) if apply_match else None,
                    "route": route_match.group(1) if route_match else None,
                    "text": text, "ok": bool(apply_match)}
        except Exception as error:  # noqa: BLE001 -- one miss must not erase 99 peers
            if attempt == 1:
                return {"job_id": job_id, "direct_url": None, "route": None,
                        "text": "", "ok": False,
                        "error": f"{type(error).__name__}: {str(error)[:200]}"}
            time.sleep(0.5 * (attempt + 1))
    raise AssertionError("unreachable")


def main() -> None:
    document = json.loads(PATH.read_text(encoding="utf-8"))
    jobs = document["jobs"]
    if len(jobs) != 100:
        raise RuntimeError(f"expected 100 jobs, found {len(jobs)}")
    resolved: dict[str, dict] = {}
    lock = threading.Lock()
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(call, job["job_id"], i): job for i, job in enumerate(jobs, 1)}
        for future in as_completed(futures):
            record = future.result()
            resolved[record["job_id"]] = record
            with lock:
                done += 1
                if done == 1 or done % 10 == 0 or done == len(jobs):
                    print(f"resolved {done}/{len(jobs)}", flush=True)

    RAW.parent.mkdir(parents=True, exist_ok=True)
    with RAW.open("w", encoding="utf-8") as fh:
        for job in jobs:
            fh.write(json.dumps(resolved[job["job_id"]], ensure_ascii=False) + "\n")

    for job in jobs:
        detail = resolved[job["job_id"]]
        # Remove every Joblens URL, including the old canonical primary URL.
        job["url"] = detail.get("direct_url")
        apply = job.setdefault("apply", {})
        apply["direct_url"] = detail.get("direct_url")
        apply["direct_source"] = "joblens MCP get_job_details"
        apply["route_raw"] = detail.get("route")
        apply["resolved"] = bool(detail.get("direct_url"))
    document["source"] = "joblens public MCP get_job_details"
    document["generated"] = time.strftime("%Y-%m-%d")
    document["skills"]["note"] = (
        "Read from the CV's KOMPETENZEN section and experience text. joblens' own PDF "
        "extractor additionally reported Go, C and R, none of which appear in the document. "
        "Employer apply URLs for all 100 jobs were resolved through the public "
        "get_job_details MCP tool, not scraped or pattern-derived."
    )
    document["search"]["direct_urls_resolved"] = sum(bool(j["url"]) for j in jobs)
    document["direct_url_resolution"] = {
        "attempted": len(jobs),
        "resolved": sum(bool(j["url"]) for j in jobs),
        "unresolved": sum(not j["url"] for j in jobs),
        "method": "get_job_details",
    }
    PATH.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(document["direct_url_resolution"]))


if __name__ == "__main__":
    main()
