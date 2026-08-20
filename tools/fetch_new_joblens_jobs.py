"""Collect 100 Joblens vacancies that have NOT been seen or applied to before.

`refresh_joblens_jobs.py` always re-fetches the same top-100 slice (fixed query, pages
1..5, sorted by match count), which is right for keeping one catalog current and useless
for finding fresh work. This walks further into the same ranked universe — and across
several role queries, because one query's tail is another's head — skipping every job id
already in `jobs.json` and every posting URL any previous run applied to.

Metadata only: search rows and the employer apply URL from `get_job_details`. It never
opens an application form and never submits anything.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / "tools"))

from refresh_joblens_jobs import (  # noqa: E402
    MIN_SECONDS_BETWEEN_REQUESTS,
    application,
    mcp_call,
    parse_detail,
    parse_search,
)

CATALOG = ROOT / "jobs.json"
OUT = ROOT / "jobs_new.json"
OUTPUTS = ROOT / "outputs"
WANTED = 100
PAGE_SIZE = 20
MAX_PAGE = 25
#: One query returns one ranked list, and its head is the slice already worked through.
#: Several role framings reach different parts of the same universe for the same CV.
QUERIES = [
    "Software Engineer", "Full Stack Developer", "Backend Engineer",
    "Cloud Engineer", "DevOps Engineer", "Platform Engineer",
    ".NET Developer", "Frontend Engineer",
]


def already_seen() -> tuple[set[str], set[str]]:
    """(job ids, posting urls) this account has already collected or applied to."""
    document = json.loads(CATALOG.read_text(encoding="utf-8"))
    ids = {str(job.get("job_id")) for job in document["jobs"] if job.get("job_id")}
    urls = {str(job["apply"].get("direct_url")) for job in document["jobs"]
            if job.get("apply", {}).get("direct_url")}
    # Every prior application run, whatever shape its record file took.
    for path in sorted(OUTPUTS.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        records = data.get("records") if isinstance(data, dict) else data
        if isinstance(data, dict) and "jobs" in data:
            records = data["jobs"]
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            apply_block = record.get("apply") if isinstance(record.get("apply"), dict) else {}
            for value in (apply_block.get("direct_url"), record.get("url_final"),
                          record.get("url")):
                if isinstance(value, str) and value.startswith("http"):
                    urls.add(value)
    return ids, urls


def main() -> None:
    document = json.loads(CATALOG.read_text(encoding="utf-8"))
    skills = document["skills"]["query_used"]
    seen_ids, seen_urls = already_seen()
    print(f"excluding {len(seen_ids)} known job ids and {len(seen_urls)} applied urls",
          flush=True)

    rows: list[dict[str, Any]] = []
    picked: set[str] = set()
    request_id = 1
    for query in QUERIES:
        if len(rows) >= WANTED:
            break
        for page in range(1, MAX_PAGE + 1):
            if len(rows) >= WANTED:
                break
            try:
                text = mcp_call("search_jobs", {"query": query, "skills": skills,
                                                "sort": "matches", "page": page}, request_id)
            except RuntimeError as error:
                print(f"  {query!r} page {page}: {error}", flush=True)
                break
            request_id += 1
            _, found = parse_search(text)
            if not found:
                break
            fresh = [row for row in found
                     if row["job_id"] not in seen_ids and row["job_id"] not in picked]
            for row in fresh:
                if len(rows) >= WANTED:
                    break
                picked.add(row["job_id"])
                row["query"] = query
                rows.append(row)
            print(f"  {query:<22} page {page:>2}: +{len(fresh):<2} -> {len(rows)}/{WANTED}",
                  flush=True)
            time.sleep(MIN_SECONDS_BETWEEN_REQUESTS)

    if len(rows) < WANTED:
        print(f"only {len(rows)} unseen vacancies available", flush=True)

    jobs: list[dict[str, Any]] = []
    for rank, row in enumerate(rows, 1):
        try:
            detail = parse_detail(mcp_call("get_job_details",
                                           {"job_id": row["job_id"]}, request_id))
        except RuntimeError as error:
            print(f"  detail {row['job_id']}: {error}", flush=True)
            detail = {"canonical_url": None, "direct_url": None, "route": None}
        request_id += 1
        url = detail["direct_url"]
        if url and url in seen_urls:
            continue                      # a different id for a posting already applied to
        jobs.append({"rank": rank, "matched": row["matched"], "company": row["company"],
                     "title": row["title"], "location": row["location"],
                     "published": row["published"],
                     "apply": application(detail["route"], url),
                     "job_id": row["job_id"], "url": url,
                     "matched_skills": row["matched_skills"], "query": row["query"]})
        if rank % 10 == 0 or rank == len(rows):
            print(f"resolved {rank}/{len(rows)}", flush=True)
        time.sleep(MIN_SECONDS_BETWEEN_REQUESTS)

    modes: dict[str, int] = {}
    for job in jobs:
        modes[job["apply"]["mode"]] = modes.get(job["apply"]["mode"], 0) + 1
    OUT.write_text(json.dumps({
        "generated": time.strftime("%Y-%m-%d"),
        "source": "joblens public MCP search_jobs + get_job_details",
        "excluded": {"known_job_ids": len(seen_ids), "applied_urls": len(seen_urls)},
        "queries": QUERIES, "skills": skills,
        "summary": {"returned": len(jobs), "modes": modes,
                    "direct_urls_resolved": sum(bool(job["url"]) for job in jobs)},
        "jobs": jobs,
    }, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"collected": len(jobs), "modes": modes,
                      "resolved": sum(bool(job["url"]) for job in jobs)}))


if __name__ == "__main__":
    main()
