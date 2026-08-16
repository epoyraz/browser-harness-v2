"""Build a direct-link test set from the latest Joblens job database.

The selection keeps up to ``--links-per-group`` recent, distinct URLs for every
employer portal. A company that publishes through multiple hostnames is split into
multiple groups so subsidiaries and separate recruiting portals remain visible.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit


def company_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "").casefold()
    return "".join(character for character in normalized if character.isalnum())


def slug(value: str, limit: int) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", normalized.casefold()).strip("-")[:limit] or "group"


def normalize_url(raw: str) -> tuple[str | None, str | None]:
    value = (raw or "").strip()
    if not value:
        return None, "blank"
    if value.casefold().startswith(("https://mailto:", "http://mailto:")):
        return None, "email_address_misencoded_as_web_url"
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        return None, "malformed_url"
    if parts.scheme.casefold() not in {"http", "https"} or not host:
        return None, "not_an_http_url"
    # Keep URL structure intact while making source rows containing literal spaces usable.
    path = quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="/%:@!$&'()*+,;=?-._~")
    fragment = quote(parts.fragment, safe="/%:@!$&'()*+,;=?-._~")
    return urlunsplit((parts.scheme.casefold(), parts.netloc, path, query, fragment)), None


def canonical_host(url: str) -> str:
    host = (urlsplit(url).hostname or "").casefold()
    return host.removeprefix("www.")


class DisjointSet:
    def __init__(self, values: set[tuple[str, str]]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: tuple[str, str]) -> tuple[str, str]:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, first: tuple[str, str], second: tuple[str, str]) -> None:
        first_root, second_root = self.find(first), self.find(second)
        if first_root != second_root:
            low, high = sorted((first_root, second_root))
            self.parent[high] = low


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, default=Path("all_jobs.sqlite"))
    parser.add_argument("--output", type=Path, default=Path("jobs.json"))
    parser.add_argument("--links-per-group", type=int, default=2)
    parser.add_argument("--source-generation")
    parser.add_argument("--source-updated-at")
    parser.add_argument("--source-md5")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.links_per_group < 1:
        raise ValueError("--links-per-group must be positive")

    connection = sqlite3.connect(args.database)
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError("SQLite integrity check failed")
    columns = "company, title, jobId, city, url, postedDate"
    source_rows = connection.execute(f"SELECT {columns} FROM jobs").fetchall()
    registry_companies = connection.execute("SELECT company FROM companies").fetchall()
    connection.close()

    rows: list[dict] = []
    rejected_by_company: dict[str, list[str]] = defaultdict(list)
    normalized_url_rows = 0
    for company, title, job_id, city, raw_url, posted_date in source_rows:
        direct_url, rejection = normalize_url(raw_url)
        if rejection:
            rejected_by_company[company].append(rejection)
            continue
        if direct_url != raw_url:
            normalized_url_rows += 1
        rows.append({
            "company": company,
            "company_key": company_key(company),
            "title": title,
            "job_id": job_id,
            "location": city,
            "published": posted_date,
            "url": direct_url,
            "source_url": raw_url if direct_url != raw_url else None,
            "host": canonical_host(direct_url),
        })

    # The normal key is company + destination host. Exact URLs shared by aliases
    # (currently Axpo/KKL) prove that those keys describe the same employer portal.
    initial_keys = {(row["company_key"], row["host"]) for row in rows}
    groups = DisjointSet(initial_keys)
    keys_by_url: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for row in rows:
        keys_by_url[row["url"]].add((row["company_key"], row["host"]))
    for keys in keys_by_url.values():
        keys = list(keys)
        for key in keys[1:]:
            groups.union(keys[0], key)

    rows_by_group: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        key = groups.find((row["company_key"], row["host"]))
        rows_by_group[key].append(row)

    group_count_by_company_key: dict[str, int] = defaultdict(int)
    for members in rows_by_group.values():
        for key in {row["company_key"] for row in members}:
            group_count_by_company_key[key] += 1

    output_jobs: list[dict] = []
    employer_groups: list[dict] = []
    shortfalls: list[dict] = []
    ordered_groups = sorted(
        rows_by_group.values(),
        key=lambda members: (
            min(row["company"].casefold() for row in members),
            min(row["host"] for row in members),
        ),
    )
    rank = 0
    for members in ordered_groups:
        aliases = sorted({row["company"] for row in members}, key=str.casefold)
        hosts = sorted({row["host"] for row in members})
        identity = "\0".join((*aliases, *hosts))
        group_id = (
            f"{slug(aliases[0], 48)}__{slug(hosts[0], 40)}__"
            f"{hashlib.sha1(identity.encode()).hexdigest()[:8]}"
        )
        ordered = sorted(
            members,
            key=lambda row: (row["published"] or "", row["job_id"] or "", row["url"]),
            reverse=True,
        )
        distinct: list[dict] = []
        seen_urls: set[str] = set()
        for row in ordered:
            if row["url"] not in seen_urls:
                distinct.append(row)
                seen_urls.add(row["url"])
        selected = distinct[: args.links_per_group]
        split = any(group_count_by_company_key[row["company_key"]] > 1 for row in members)
        for row in selected:
            rank += 1
            job = {
                "rank": rank,
                "employer_group_id": group_id,
                "employer_site": row["host"],
                "company": row["company"],
                "company_aliases": aliases,
                "title": row["title"],
                "location": row["location"],
                "published": row["published"],
                "job_id": row["job_id"],
                "url": row["url"],
                "apply": {"direct_url": row["url"], "resolved": True},
            }
            if row["source_url"]:
                job["source_url"] = row["source_url"]
            output_jobs.append(job)
        group_record = {
            "employer_group_id": group_id,
            "company_aliases": aliases,
            "employer_sites": hosts,
            "subsidiary_or_portal_split": split,
            "available_distinct_links": len(distinct),
            "selected_links": len(selected),
            "selected_job_ids": [row["job_id"] for row in selected],
        }
        employer_groups.append(group_record)
        if len(selected) < args.links_per_group:
            shortfalls.append(group_record)

    registry_names = sorted({row[0] for row in registry_companies}, key=str.casefold)
    covered_companies = {row["company"] for row in rows}
    exclusions = []
    for company in registry_names:
        if company not in covered_companies:
            reasons = sorted(set(rejected_by_company.get(company) or ["no_valid_direct_url"]))
            exclusions.append({"company": company, "reasons": reasons})

    all_urls = [job["url"] for job in output_jobs]
    if len(all_urls) != len(set(all_urls)):
        raise RuntimeError("selected URLs are not globally unique")
    if any("joblens.ch" in urlsplit(url).hostname.casefold() for url in all_urls):
        raise RuntimeError("Joblens URL leaked into direct-link test set")
    if any(job["apply"]["direct_url"] != job["url"] for job in output_jobs):
        raise RuntimeError("primary and apply URLs disagree")

    document = {
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": {
            "gcs_object": "gs://jobboard-data-exports/latest/all_jobs.sqlite",
            "generation": args.source_generation,
            "updated_at": args.source_updated_at,
            "md5": args.source_md5,
            "local_database": str(args.database.resolve()),
            "sqlite_integrity_check": "ok",
        },
        "selection": {
            "links_per_employer_group_target": args.links_per_group,
            "grouping": (
                "Normalize company label and split it by canonical destination hostname; "
                "merge aliases only when they share an exact direct job URL."
            ),
            "ordering": "Newest postedDate first, then jobId and URL descending.",
            "direct_links_only": True,
            "joblens_links": 0,
        },
        "summary": {
            "source_jobs": len(source_rows),
            "source_company_registry": len(registry_names),
            "companies_with_valid_direct_links": len(covered_companies),
            "companies_without_valid_direct_links": len(exclusions),
            "employer_groups": len(employer_groups),
            "groups_meeting_link_target": len(employer_groups) - len(shortfalls),
            "groups_below_link_target": len(shortfalls),
            "selected_jobs": len(output_jobs),
            "distinct_direct_urls": len(set(all_urls)),
            "source_rows_rejected": len(source_rows) - len(rows),
            "source_urls_normalized": normalized_url_rows,
        },
        "exclusions": exclusions,
        "shortfalls": shortfalls,
        "employer_groups": employer_groups,
        "jobs": output_jobs,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    serialized = json.dumps(document, indent=1, ensure_ascii=False) + "\n"
    temporary.write_bytes(serialized.encode("utf-8"))
    os.replace(temporary, args.output)
    print(json.dumps(document["summary"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
