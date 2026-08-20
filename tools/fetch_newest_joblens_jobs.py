"""Collect the 100 most recently published Joblens vacancies for this CV.

Different axis from the other two collectors: `refresh_joblens_jobs.py` takes the top
slice by skill-match count and `fetch_new_joblens_jobs.py` takes whatever the account has
not seen. This takes the newest, `sort="date"`, because a posting's age decides whether
applying is still worth anything. Skills are still passed so each row carries its matched
count, and every entry is flagged `seen` when the catalog or a previous run already had it.

Metadata only: search rows plus the employer apply URL from `get_job_details`. It never
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

from fetch_new_joblens_jobs import already_seen  # noqa: E402
from refresh_joblens_jobs import (  # noqa: E402
    MIN_SECONDS_BETWEEN_REQUESTS,
    application,
    mcp_call,
    parse_detail,
    parse_search,
)

CATALOG = ROOT / "jobs.json"
OUT = ROOT / "jobs_newest.json"
WANTED = 100
QUERY = "Software Engineer"
MAX_PAGE = 25


def main() -> None:
    document = json.loads(CATALOG.read_text(encoding="utf-8"))
    skills = document["skills"]["query_used"]
    seen_ids, seen_urls = already_seen()

    rows: list[dict[str, Any]] = []
    picked: set[str] = set()
    request_id = 1
    for page in range(1, MAX_PAGE + 1):
        if len(rows) >= WANTED:
            break
        text = mcp_call("search_jobs", {"query": QUERY, "skills": skills,
                                        "sort": "date", "page": page}, request_id)
        request_id += 1
        _, found = parse_search(text)
        if not found:
            break
        for row in found:
            if len(rows) >= WANTED or row["job_id"] in picked:
                continue
            picked.add(row["job_id"])
            rows.append(row)
        newest = rows[0].get("published") if rows else None
        print(f"  page {page:>2}: {len(rows)}/{WANTED}  newest={newest}", flush=True)
        time.sleep(MIN_SECONDS_BETWEEN_REQUESTS)

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
        jobs.append({"rank": rank, "matched": row["matched"], "company": row["company"],
                     "title": row["title"], "location": row["location"],
                     "published": row["published"],
                     "apply": application(detail["route"], url),
                     "job_id": row["job_id"], "url": url,
                     "matched_skills": row["matched_skills"],
                     # Already in the catalog, or already applied to in an earlier run.
                     "seen": row["job_id"] in seen_ids or (bool(url) and url in seen_urls)})
        if rank % 20 == 0 or rank == len(rows):
            print(f"resolved {rank}/{len(rows)}", flush=True)
        time.sleep(MIN_SECONDS_BETWEEN_REQUESTS)

    modes: dict[str, int] = {}
    for job in jobs:
        modes[job["apply"]["mode"]] = modes.get(job["apply"]["mode"], 0) + 1
    dates = [job["published"] for job in jobs if job["published"]]
    OUT.write_text(json.dumps({
        "generated": time.strftime("%Y-%m-%d"),
        "source": "joblens public MCP search_jobs(sort=date) + get_job_details",
        "query": QUERY, "sort": "date", "skills": skills,
        "summary": {"returned": len(jobs), "modes": modes,
                    "published_range": [min(dates), max(dates)] if dates else None,
                    "already_seen": sum(job["seen"] for job in jobs),
                    "direct_urls_resolved": sum(bool(job["url"]) for job in jobs)},
        "jobs": jobs,
    }, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"collected": len(jobs), "modes": modes,
                      "published_range": [min(dates), max(dates)] if dates else None,
                      "already_seen": sum(job["seen"] for job in jobs)}))


if __name__ == "__main__":
    main()
