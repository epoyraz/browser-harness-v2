"""Refresh ``jobs.json`` from Joblens's public, stateless MCP endpoint.

The search terms are read from the document's CV-derived ``skills.query_used``
field.  It intentionally only collects vacancy metadata and employer apply URLs;
it never opens an application form or submits anything.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "jobs.json"
ENDPOINT = "https://joblens.ch/api/mcp"
PAGE_SIZE = 20
LIMIT = 100
# Joblens documents a 60 request/minute rate limit for unverified MCP clients.
MIN_SECONDS_BETWEEN_REQUESTS = 1.1


def mcp_call(name: str, arguments: dict[str, Any], request_id: int) -> str:
    """Call one MCP tool, keeping below the public unauthenticated rate limit."""
    payload = json.dumps({
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    response = subprocess.run(
        ["curl", "-fsS", "--retry", "3", "--retry-all-errors", "--retry-delay", "2",
         ENDPOINT,
         "-H", "Content-Type: application/json",
         "-H", "Accept: application/json, text/event-stream",
         "-H", "Mcp-Protocol-Version: 2025-03-26",
         "-H", "User-Agent: browser-harness-job-refresh/0.1",
         "--data-binary", "@-"],
        # encoding is explicit: `text=True` alone decodes with the LOCALE, which on
        # Windows is cp1252 — every em dash and umlaut in a Joblens row arrived as
        # mojibake ("**—" -> "**â€”") and the row parser rejected it.
        input=payload, text=True, encoding="utf-8", capture_output=True, timeout=90, check=False,
    )
    if response.returncode:
        raise RuntimeError(response.stderr.strip() or f"curl exited {response.returncode}")
    data = json.loads(response.stdout)
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    content = (data.get("result") or {}).get("content") or []
    text = "\n".join(str(item.get("text") or "") for item in content
                     if item.get("type") == "text")
    if not text:
        raise RuntimeError(f"empty {name} response: {data!r}")
    return text


def parse_search(text: str) -> tuple[int, list[dict[str, Any]]]:
    total_match = re.search(r"(?:Found|found)\s+([\d,]+)\s+jobs|^([\d,]+)\s+jobs\s+mention", text)
    if not total_match:
        raise RuntimeError("Joblens search result did not report a total")
    total = int(next(value for value in total_match.groups() if value).replace(",", ""))
    chunks = re.split(r"(?m)^\d+\. \*\*", text)[1:]
    jobs: list[dict[str, Any]] = []
    for chunk in chunks:
        header, _, rest = chunk.partition("\n")
        title, sep, source = header.partition("** — ")
        if not sep:
            raise RuntimeError(f"unrecognised Joblens search row: {header!r}")
        fields = [part.strip() for part in source.split(" · ")]
        company = fields[0]
        published = fields.pop() if fields and re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields[-1]) else None
        location = " · ".join(fields[1:]) or None
        matched = re.search(r"(?m)^\s*(\d+) matched:\s*(.+?)\s*$", rest)
        identifier = re.search(r"(?m)^\s*jobId:\s*(\S+)\s*·", rest)
        if not matched or not identifier:
            raise RuntimeError(f"incomplete Joblens search row for {title!r}")
        jobs.append({
            "title": title.strip(), "company": company, "location": location,
            "published": published, "matched": int(matched.group(1)),
            "matched_skills": [skill.strip() for skill in matched.group(2).split(",")],
            "job_id": identifier.group(1),
        })
    return total, jobs


def parse_detail(text: str) -> dict[str, str | None]:
    def field(label: str) -> str | None:
        match = re.search(rf"(?m)^{re.escape(label)}:\s*(\S+)\s*$", text)
        return match.group(1) if match else None

    route_match = re.search(r"(?m)^Application route:\s*(.+?)\s*$", text)
    return {
        "canonical_url": field("Canonical URL"),
        "direct_url": field("Apply (employer site)"),
        "route": route_match.group(1) if route_match else None,
    }


def application(route: str | None, direct_url: str | None) -> dict[str, Any]:
    if route and route.startswith("direct form, no account"):
        mode = "form"
    elif route and route.startswith("account required"):
        mode = "account"
    else:
        mode = "unknown"
    ats = None
    if route:
        match = re.search(r"\(([^()]+)\)$", route)
        ats = match.group(1) if match else None
    return {
        "mode": mode, "ats": ats, "direct_url": direct_url,
        "direct_source": "joblens MCP get_job_details", "route_raw": route,
        "resolved": bool(direct_url),
    }


def main() -> None:
    document = json.loads(PATH.read_text(encoding="utf-8"))
    query = document["skills"]["query_used"]
    search_jobs: list[dict[str, Any]] = []
    total = 0
    request_id = 1
    for page in range(1, LIMIT // PAGE_SIZE + 1):
        text = mcp_call("search_jobs", {
            "query": "Software Engineer", "skills": query, "sort": "matches", "page": page,
        }, request_id)
        request_id += 1
        page_total, rows = parse_search(text)
        total = page_total
        search_jobs.extend(rows)
        print(f"searched page {page}: {len(search_jobs)}/{LIMIT}", flush=True)
        time.sleep(MIN_SECONDS_BETWEEN_REQUESTS)
    if len(search_jobs) != LIMIT:
        raise RuntimeError(f"expected {LIMIT} search rows, got {len(search_jobs)}")

    jobs: list[dict[str, Any]] = []
    for rank, row in enumerate(search_jobs, 1):
        text = mcp_call("get_job_details", {"job_id": row["job_id"]}, request_id)
        request_id += 1
        detail = parse_detail(text)
        route = application(detail["route"], detail["direct_url"])
        jobs.append({"rank": rank, "matched": row["matched"], "company": row["company"],
                     "title": row["title"], "location": row["location"],
                     "published": row["published"], "apply": route,
                     "job_id": row["job_id"], "url": detail["direct_url"],
                     "matched_skills": row["matched_skills"]})
        if rank == 1 or rank % 10 == 0 or rank == LIMIT:
            print(f"resolved {rank}/{LIMIT}", flush=True)
        time.sleep(MIN_SECONDS_BETWEEN_REQUESTS)

    document["generated"] = time.strftime("%Y-%m-%d")
    document["source"] = "joblens public MCP search_jobs + get_job_details"
    document["skills"]["note"] = (
        "Read from the CV's KOMPETENZEN section and experience text. Joblens ranks "
        "the vacancy descriptions against the listed CV-derived skills. Employer apply "
        "URLs were resolved through the public get_job_details MCP tool, not scraped "
        "or pattern-derived."
    )
    document["search"] = {
        "city_filter": None, "sort": "matches", "universe_matching_at_least_one_skill": total,
        "returned": len(jobs), "direct_urls_resolved": sum(bool(job["url"]) for job in jobs),
    }
    document["direct_url_resolution"] = {
        "attempted": len(jobs), "resolved": sum(bool(job["url"]) for job in jobs),
        "unresolved": sum(not job["url"] for job in jobs), "method": "get_job_details",
    }
    document["summary"] = {
        "direct_form_no_account": sum(job["apply"]["mode"] == "form" for job in jobs),
        "account_required": sum(job["apply"]["mode"] == "account" for job in jobs),
        "not_reported": sum(job["apply"]["mode"] == "unknown" for job in jobs),
        "top_companies": [f"{company} ({count})" for company, count in sorted(
            ((company, sum(job["company"] == company for job in jobs))
             for company in {job["company"] for job in jobs}), key=lambda item: (-item[1], item[0]))[:6]],
    }
    document["jobs"] = jobs
    PATH.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"total": total, **document["direct_url_resolution"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
