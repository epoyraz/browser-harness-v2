"""Paired comparison of experiment arms (same postings, joined on job_id).

    python analyze.py out/E01/C-1 out/E01/T-1 [more arms...]   # first arm is the control
    python analyze.py --pool out/E*/C-*                          # noise estimate across controls
"""
from __future__ import annotations

import argparse
import json
import statistics as st
from collections import Counter, defaultdict
from pathlib import Path

FORM = "form_processed"


def load_arm(path: Path) -> dict:
    res = json.loads((path / "results.json").read_text(encoding="utf-8"))
    meta = json.loads((path / "arm.json").read_text(encoding="utf-8")) if (path / "arm.json").exists() else {}
    values = {}
    for rec in res["records"]:
        v = rec.get("value") or {}
        jid = v.get("job_id") or (rec.get("item") or {}).get("job_id")
        if not jid:
            continue
        if not rec.get("ok") and not v:
            v = {"job_id": jid, "status": "record_failed", "error_class": rec.get("class"), "wall_ms": 0}
        values[jid] = v
    # journal: helper spans per posting
    cdp = defaultdict(int); helper_ms = defaultdict(float); fn_counts = defaultdict(Counter)
    jp = path / "journal.jsonl"
    if jp.exists():
        for line in jp.open(encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("kind") != "call" or not r.get("item_id"):
                continue
            cdp[r["item_id"]] += int(r.get("cdp") or 0)
            helper_ms[r["item_id"]] += float(r.get("ms") or 0)
            fn_counts[r["item_id"]][r.get("fn")] += 1
    return {"path": str(path), "meta": meta, "values": values, "cdp": cdp, "helper_ms": helper_ms,
            "fn_counts": fn_counts, "wall_ms": res.get("meta", {}).get("wall_ms"),
            "timing": res.get("timing_summary") or {}}


def attempted(v: dict) -> bool:
    return v.get("status") not in ("skipped_by_metadata", "skipped_by_memo")


def summary(arm: dict) -> dict:
    vals = list(arm["values"].values())
    att = [v for v in vals if attempted(v)]
    statuses = Counter(v.get("status") for v in vals)
    return {"n": len(vals), "attempted": len(att), "forms": statuses.get(FORM, 0), "statuses": dict(statuses),
            "wall_s": round((arm["wall_ms"] or 0) / 1000, 1),
            "attempt_s_sum": round(sum(v.get("wall_ms", 0) for v in att) / 1000, 1),
            "attempt_s_median": round(st.median([v.get("wall_ms", 0) for v in att]) / 1000, 2) if att else None,
            "attempt_s_p95": round(sorted(v.get("wall_ms", 0) for v in att)[int(0.95 * (len(att) - 1))] / 1000, 2) if att else None,
            "navigate_s_median": round(st.median([v.get("navigate_ms") or 0 for v in att]) / 1000, 2) if att else None,
            "cdp_per_posting_median": st.median([arm["cdp"].get(v["job_id"], 0) for v in att]) if att and arm["cdp"] else None,
            "cdp_sum": sum(arm["cdp"].values()) or None,
            "cleanup_ms_sum": round(arm["timing"].get("sum_cleanup_target_query_ms") or 0),
            "terminal": dict(Counter(v.get("workflow_terminal") for v in att)),
            # skills experiment: what the hints did
            "skipped_by_skill": statuses.get("skipped_by_skill", 0),
            "hinted": sum(1 for v in vals if (v.get("skill_hints") or {}).get("ids")),
            "routes_offered": sum(1 for v in vals if v.get("skill_routes")),
            "route_first_hits": sum(1 for v in att for h in (v.get("hops") or [])[:1]
                                    if isinstance(h, dict) and h.get("via") == "route_rule" and h.get("accepted")),
            "hops_to_form_mean": round(st.mean([len(v.get("hops") or []) for v in att if v.get("status") == FORM]), 2)
            if any(v.get("status") == FORM for v in att) else None,
            # action cache: postings where the clicked apply control was the hinted one
            "hinted_clicks": sum(1 for v in att for h in (v.get("hops") or [])
                                 if isinstance(h, dict) and (h.get("apply_control") or {}).get("hinted")
                                 and (h.get("transition") or {}).get("kind") in ("control", "new_target")),
            "hinted_clicks_to_form": sum(1 for v in att if v.get("status") == FORM
                                         for h in (v.get("hops") or [])
                                         if isinstance(h, dict) and (h.get("apply_control") or {}).get("hinted")
                                         and (h.get("transition") or {}).get("kind") in ("control", "new_target"))}


def paired(control: dict, treat: dict) -> dict:
    ids = [j for j in control["values"] if j in treat["values"]
           and attempted(control["values"][j]) and attempted(treat["values"][j])]
    flips = Counter(); gained = []; lost = []; d_ms = []; d_cdp = []; d_nav = []
    for j in ids:
        a, b = control["values"][j], treat["values"][j]
        if a.get("status") != b.get("status"):
            flips[f"{a.get('status')} -> {b.get('status')}"] += 1
        if a.get("status") != FORM and b.get("status") == FORM:
            gained.append(j)
        if a.get("status") == FORM and b.get("status") != FORM:
            lost.append(j)
        d_ms.append((b.get("wall_ms") or 0) - (a.get("wall_ms") or 0))
        d_nav.append((b.get("navigate_ms") or 0) - (a.get("navigate_ms") or 0))
        if control["cdp"] and treat["cdp"]:
            d_cdp.append(treat["cdp"].get(j, 0) - control["cdp"].get(j, 0))
    return {"matched_n": len(ids), "same_outcome": len(ids) - sum(flips.values()), "flips": dict(flips),
            "forms_gained": gained, "forms_lost": lost,
            "attempt_s_delta_sum": round(sum(d_ms) / 1000, 1),
            "attempt_s_delta_median": round(st.median(d_ms) / 1000, 2) if d_ms else None,
            "navigate_s_delta_median": round(st.median(d_nav) / 1000, 2) if d_nav else None,
            "cdp_delta_sum": sum(d_cdp) if d_cdp else None,
            "cdp_delta_median": st.median(d_cdp) if d_cdp else None}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("arms", nargs="+")
    ap.add_argument("--pool", action="store_true", help="treat every arm as a replicate; report spread")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    paths = []
    for a in args.arms:
        p = Path(a)
        paths.extend(sorted(p.parent.glob(p.name)) if any(c in a for c in "*?") else [p])
    arms = [load_arm(p) for p in paths if (p / "results.json").exists()]
    out = {"arms": {Path(a["path"]).name: summary(a) for a in arms}}
    if args.pool and len(arms) > 1:
        forms = [summary(a)["forms"] for a in arms]; walls = [summary(a)["wall_s"] for a in arms]
        pairs = [paired(arms[i], arms[j]) for i in range(len(arms)) for j in range(i + 1, len(arms))]
        out["pool"] = {"forms": forms, "wall_s": walls,
                       "pairwise_flips": [sum(p["flips"].values()) for p in pairs],
                       "pairwise_form_moves": [len(p["forms_gained"]) + len(p["forms_lost"]) for p in pairs]}
    elif len(arms) > 1:
        out["paired"] = {Path(a["path"]).name: paired(arms[0], a) for a in arms[1:]}
    if args.json:
        print(json.dumps(out, indent=1, default=str))
    else:
        for name, s in out["arms"].items():
            print(f"{name:22} forms={s['forms']:3} att={s['attempted']:3} wall={s['wall_s']:7}s attempt_sum={s['attempt_s_sum']:7}s "
                  f"med={s['attempt_s_median']} p95={s['attempt_s_p95']} nav_med={s['navigate_s_median']} cdp_med={s['cdp_per_posting_median']} cleanup={s['cleanup_ms_sum']}ms")
            print(f"{'':22} statuses={s['statuses']}")
            if s["hinted"] or s["skipped_by_skill"]:
                print(f"{'':22} skills: hinted={s['hinted']} skipped_by_skill={s['skipped_by_skill']} routes_offered={s['routes_offered']} route_first_hits={s['route_first_hits']} hops_to_form_mean={s['hops_to_form_mean']} hinted_clicks={s['hinted_clicks']} (to form {s['hinted_clicks_to_form']})")
        for name, p in out.get("paired", {}).items():
            print(f"PAIRED {name}: n={p['matched_n']} same={p['same_outcome']} gained={len(p['forms_gained'])} lost={len(p['forms_lost'])} "
                  f"attempt_delta_sum={p['attempt_s_delta_sum']}s median={p['attempt_s_delta_median']}s nav_med={p['navigate_s_delta_median']}s cdp_delta={p['cdp_delta_sum']} (med {p['cdp_delta_median']})")
            if p["flips"]:
                print(f"       flips={p['flips']}")
            if p["forms_lost"]:
                print(f"       lost={p['forms_lost']}")
            if p["forms_gained"]:
                print(f"       gained={p['forms_gained']}")
        if "pool" in out:
            print("POOL", out["pool"])


if __name__ == "__main__":
    main()
