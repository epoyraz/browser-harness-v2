"""Summarise a recordings library: one line per employer host.

    python recordings_report.py recordings            # the 500-employer library
    python recordings_report.py replay-headed --json  # the corpus experiment's store
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "recordings")
    as_json = "--json" in sys.argv
    hosts = {}
    for p in sorted(root.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        recs = data if isinstance(data, list) else [data]
        hosts[p.stem] = recs
    n_hosts = len(hosts)
    n_recs = sum(len(v) for v in hosts.values())
    steps = Counter(len(r.get("steps") or []) for v in hosts.values() for r in v)
    fields = [len(r.get("fields") or []) for v in hosts.values() for r in v]
    used = [r for v in hosts.values() for r in v if (r.get("stats") or {}).get("uses")]
    successes = sum((r.get("stats") or {}).get("successes", 0) for r in used)
    uses = sum((r.get("stats") or {}).get("uses", 0) for r in used)
    retired = [r for v in hosts.values() for r in v if (r.get("stats") or {}).get("consecutive_failures", 0) >= 2]
    semantics = Counter(f.get("semantic") for v in hosts.values() for r in v for f in (r.get("fields") or []))
    gaps = Counter((u.get("semantic") or u.get("label")) for v in hosts.values() for r in v
                   for u in (r.get("required_unplanned") or []) if isinstance(u, dict))
    summary = {"library": str(root), "employer_hosts": n_hosts, "recordings": n_recs,
               "steps_distribution": dict(steps), "fields_median": sorted(fields)[len(fields) // 2] if fields else None,
               "replays": uses, "replay_successes": successes, "retired": len(retired),
               "top_semantics": semantics.most_common(12), "top_unplanned_required": gaps.most_common(10)}
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        return
    print(json.dumps({k: v for k, v in summary.items() if k not in ("top_semantics", "top_unplanned_required")}, ensure_ascii=False))
    print("top semantics:", summary["top_semantics"])
    print("required fields discovery could not plan (need input):", summary["top_unplanned_required"])
    for host, recs in sorted(hosts.items())[:40]:
        for r in recs:
            st = r.get("stats") or {}
            print(f"  {host[:34]:34} {r.get('company','')[:22]:22} steps={len(r.get('steps') or [])} fields={len(r.get('fields') or [])} "
                  f"gaps={len(r.get('required_unplanned') or [])} uses={st.get('uses',0)} ok={st.get('successes',0)} fp={r.get('fingerprint')}")


if __name__ == "__main__":
    main()
