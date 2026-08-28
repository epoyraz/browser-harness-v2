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

import itertools
import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / "tools"))

from refresh_joblens_jobs import (
    MIN_SECONDS_BETWEEN_REQUESTS,
    application,
    mcp_call,
    parse_detail,
    parse_search,
)

CATALOG = ROOT / "jobs.json"
OUT = Path(os.environ.get("BH_FETCH_OUT") or ROOT / "jobs_new.json")
OUTPUTS = ROOT / "outputs"
WANTED = int(os.environ.get("BH_FETCH_WANTED") or 100)
#: One posting per employer. A ranked search returns the same employer many times — 100
#: postings measured 2026-08-27 were only 54 companies, and the 38 that filled were 21 —
#: so a run that wants N *distinct* companies has to reject repeats while collecting,
#: not afterwards, or it pays for details it will throw away.
ONE_PER_COMPANY = (os.environ.get("BH_FETCH_ONE_PER_COMPANY", "").strip().lower()
                   in {"1", "true", "yes"})
PAGE_SIZE = 20
MAX_PAGE = 25
#: One query returns one ranked list, and its head is the slice already worked through.
#: Several role framings reach different parts of the same universe for the same CV.
QUERIES = [
    "Software Engineer", "Full Stack Developer", "Backend Engineer",
    "Cloud Engineer", "DevOps Engineer", "Platform Engineer",
    ".NET Developer", "Frontend Engineer",
]
if extra := os.environ.get("BH_FETCH_QUERIES"):
    QUERIES = [q.strip() for q in extra.split(",") if q.strip()]
#: Continue an earlier corpus instead of starting over. A ranked search returns the same
#: employers to every query, so a second pass with wider queries re-walks pages it has
#: already paid for unless it is told which companies and ids the first pass took.
EXTEND = Path(os.environ["BH_FETCH_EXTEND"]) if os.environ.get("BH_FETCH_EXTEND") else None


def _record_urls(record: Any) -> Iterator[str]:
    """Posting URLs one run record carries, whatever shape that run wrote.

    A telemetry run nests the whole job under `item` and keeps the outcome beside it, so
    the same posting is one level deeper than in a plain application record.
    """
    if not isinstance(record, dict):
        return
    for candidate in (record, record.get("item")):
        if not isinstance(candidate, dict):
            continue
        apply_block = (candidate.get("apply")
                       if isinstance(candidate.get("apply"), dict) else {})
        for value in (apply_block.get("direct_url"), candidate.get("url_final"),
                      candidate.get("url")):
            if isinstance(value, str) and value.startswith("http"):
                yield value


def already_seen() -> tuple[set[str], set[str]]:
    """(job ids, posting urls) this account has already collected or applied to."""
    document = json.loads(CATALOG.read_text(encoding="utf-8"))
    ids = {str(job.get("job_id")) for job in document["jobs"] if job.get("job_id")}
    urls = {str(job["apply"].get("direct_url")) for job in document["jobs"]
            if job.get("apply", {}).get("direct_url")}
    # Every prior application run, whatever shape its record file took. The second glob is
    # not redundant: a telemetry run writes `results.json` inside its own directory, and
    # measured 2026-08-27, 22 of the 100 postings in the last 100-job run were reachable
    # only there. Without them a "new" list re-serves postings already applied to.
    for path in sorted(OUTPUTS.glob("*.json")) + sorted(OUTPUTS.glob("*/results.json")):
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
            urls.update(_record_urls(record))
    return ids, urls


def _candidates(skills: str, seen_ids: set[str], counter: Iterator[int],
                seen_companies: set[str] | None = None) -> Iterator[dict[str, Any]]:
    """Walk the ranked universe lazily, yielding rows this account has not collected.

    Lazy on purpose. A search row only becomes a usable vacancy once `get_job_details`
    resolves an apply URL that no earlier run touched, and measured 2026-08-27 only 19 of
    100 rows survived that second filter. Collecting a fixed 100 rows up front therefore
    produced a list of 19; the caller has to be able to keep pulling until it has as many
    *resolved* jobs as it asked for.
    """
    picked: set[str] = set()
    companies: set[str] = set(seen_companies or ())
    for query in QUERIES:
        for page in range(1, MAX_PAGE + 1):
            try:
                text = mcp_call("search_jobs", {"query": query, "skills": skills,
                                                "sort": "matches", "page": page},
                                next(counter))
            except RuntimeError as error:
                print(f"  {query!r} page {page}: {error}", flush=True)
                break
            _, found = parse_search(text)
            if not found:
                break
            fresh = []
            for row in found:
                if row["job_id"] in seen_ids or row["job_id"] in picked:
                    continue
                company = str(row.get("company") or "").strip().casefold()
                if ONE_PER_COMPANY and company and company in companies:
                    continue
                if company:
                    companies.add(company)
                fresh.append(row)
            print(f"  {query:<22} page {page:>2}: +{len(fresh)}", flush=True)
            time.sleep(MIN_SECONDS_BETWEEN_REQUESTS)
            for row in fresh:
                picked.add(row["job_id"])
                row["query"] = query
                yield row


def main() -> None:
    document = json.loads(CATALOG.read_text(encoding="utf-8"))
    skills = document["skills"]["query_used"]
    seen_ids, seen_urls = already_seen()
    print(f"excluding {len(seen_ids)} known job ids and {len(seen_urls)} applied urls",
          flush=True)

    carried: list[dict[str, Any]] = []
    if EXTEND and EXTEND.is_file():
        carried = json.loads(EXTEND.read_text(encoding="utf-8"))["jobs"]
        seen_ids = seen_ids | {str(j.get("job_id")) for j in carried if j.get("job_id")}
        seen_urls = seen_urls | {str(j["url"]) for j in carried if j.get("url")}
        print(f"extending {EXTEND.name}: {len(carried)} jobs, "
              f"{len({j['company'] for j in carried})} companies already held", flush=True)
    held = {str(j.get("company") or "").strip().casefold() for j in carried}

    counter = itertools.count(1)
    jobs: list[dict[str, Any]] = list(carried)
    for row in _candidates(skills, seen_ids, counter, held):
        if len(jobs) >= WANTED:
            break
        try:
            detail = parse_detail(mcp_call("get_job_details",
                                           {"job_id": row["job_id"]}, next(counter)))
        except RuntimeError as error:
            print(f"  detail {row['job_id']}: {error}", flush=True)
            detail = {"canonical_url": None, "direct_url": None, "route": None}
        time.sleep(MIN_SECONDS_BETWEEN_REQUESTS)
        url = detail["direct_url"]
        if url and url in seen_urls:
            continue                      # a different id for a posting already applied to
        jobs.append({"rank": len(jobs) + 1, "matched": row["matched"],
                     "company": row["company"],
                     "title": row["title"], "location": row["location"],
                     "published": row["published"],
                     "apply": application(detail["route"], url),
                     "job_id": row["job_id"], "url": url,
                     "matched_skills": row["matched_skills"], "query": row["query"]})
        if len(jobs) % 10 == 0:
            print(f"resolved {len(jobs)}/{WANTED}", flush=True)

    if len(jobs) < WANTED:
        print(f"only {len(jobs)} unseen vacancies available", flush=True)

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
