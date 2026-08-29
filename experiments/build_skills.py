"""Build skill corpora for the with/without-skills experiment.

    python build_skills.py static                 # vendor + company skills from the ATS map
    python build_skills.py cache out/ctl/C-08     # + learned routes from one run's results.json

Each corpus is a BH_CONFIG_HOME directory holding sources.toml and path sources:
    skills/<corpus>/sources.toml
    skills/<corpus>/ats/<vendor>.md        priority 60
    skills/<corpus>/company/<host>.md      priority 80
    skills/<corpus>/learned/<host>.md      priority 100   (cache corpus only)
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
ATS = HERE.parent / "ats-map"
V2 = HERE.parent / "v2"
sys.path.insert(0, str(ATS))
from stage4_classify import VENDORS

#: Vendors whose application pages paint nothing in a hidden tab (measured 2026-08-29).
RENDERS_HIDDEN_FALSE = {"Abacus Umantis", "pastaHR", "Workday"}


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def mode_of(application_type: str) -> str | None:
    t = application_type or ""
    if t.startswith("Fillable form"):
        return "form"
    if t.startswith("Account required"):
        return "account"
    if t.startswith(("Email", "PDF")):
        return "email"
    return None


def vendor_for_host(host: str) -> str | None:
    for pat, name, _kind in VENDORS:
        if re.search(pat, host):
            return name
    return None


def load_atsmap() -> list[dict]:
    return json.load(open(ATS / "ats_map_final.json", encoding="utf-8"))


def chain_hosts() -> dict[str, set[str]]:
    """vendor -> hosts seen anywhere in the browser chains."""
    out: dict[str, set[str]] = defaultdict(set)
    for f in ("stage3_browser.jsonl",):
        for line in open(ATS / f, encoding="utf-8"):
            if not line.strip():
                continue
            rec = json.loads(line)
            for hop in rec.get("hops") or []:
                host = (urlsplit(hop.get("landed") or "").hostname or "").lower()
                v = vendor_for_host(host)
                if v and host:
                    out[v].add(host)
    return out


def write_skill(path: Path, skill_id: str, description: str, matches: list[dict], apply: dict, prose: str) -> None:
    lines = ["---", f"id: {skill_id}", "version: 2026.08.29", f"description: {description}", "match:"]
    for m in matches:
        for k, v in m.items():
            lines.append(f"  - {k}: \"{v}\"")
    lines += ["---", "", prose.strip(), "", "```json", json.dumps({"apply": apply}, ensure_ascii=False, indent=1), "```", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def build_static(root: Path) -> dict:
    rows = load_atsmap()
    by_vendor: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_vendor[r["ats_family"]].append(r)
    hosts_by_vendor = chain_hosts()
    n_ats = 0
    for vendor, recs in by_vendor.items():
        if vendor in ("Custom / in-house", "Unknown"):
            continue
        modes = Counter(m for m in (mode_of(r["application_type"]) for r in recs) if m)
        if not modes:
            continue
        mode, n = modes.most_common(1)[0]
        confidence = n / sum(modes.values())
        hosts = set(hosts_by_vendor.get(vendor, set()))
        for r in recs:
            h = (urlsplit(r.get("final_application_url") or "").hostname or "").lower()
            if h and vendor_for_host(h) == vendor:
                hosts.add(h)
        # generic globs for the vendor's own domains + every exact host observed
        globs = sorted({"*." + ".".join(h.split(".")[-2:]) for h in hosts if vendor_for_host("x." + ".".join(h.split(".")[-2:])) == vendor})
        matches = [{"host": g} for g in globs] + [{"host": h} for h in sorted(hosts) if not any(re.fullmatch(g.replace("*", ".*"), h) for g in globs)]
        if not matches:
            continue
        apply = {"mode": mode, "ats": vendor, "mode_confidence": round(confidence, 2), "companies_observed": len(recs)}
        if vendor in RENDERS_HIDDEN_FALSE:
            apply["renders_hidden"] = False
        prose = (f"# {vendor}\n\nObserved on {len(recs)} employers in the joblens top-500 map (2026-08-29): "
                 f"{dict(modes)}. Typical flow: **{mode}**." + (" The application page paints only in a visible tab — activate it." if vendor in RENDERS_HIDDEN_FALSE else ""))
        write_skill(root / "ats" / f"{slug(vendor)}.md", f"ats/{slug(vendor)}", f"{vendor}: {mode} ({n}/{sum(modes.values())} employers)", matches, apply, prose)
        n_ats += 1
    # company skills: the exact final host of every employer with a decided mode
    n_co = 0
    for r in rows:
        h = (urlsplit(r.get("final_application_url") or "").hostname or "").lower()
        m = mode_of(r["application_type"])
        if not h or not m or r["confidence"] == "low":
            continue
        apply = {"mode": m, "ats": r["ats_family"] if r["ats_family"] not in ("Custom / in-house", "Unknown") else None, "company": r["company"]}
        if r["ats_family"] in RENDERS_HIDDEN_FALSE:
            apply["renders_hidden"] = False
        write_skill(root / "company" / f"{slug(h)}.md", f"company/{slug(h)}", f"{r['company']}: {r['application_type']}",
                    [{"host": h}], apply, f"# {r['company']}\n\n{r['application_type']} via {r['ats']} (chain: {r['chain'][:200]}).")
        n_co += 1
    return {"ats": n_ats, "company": n_co}


def build_learned(root: Path, results_paths: list[Path]) -> dict:
    """Routes learned from runs: start_url -> the URL where the form was found."""
    routes_by_host: dict[str, list[dict]] = defaultdict(list)
    observed: Counter = Counter()          # (start, landed) -> runs in which it was seen
    hops_seen: dict[tuple[str, str], int] = {}
    for rp in results_paths:
        d = json.load(open(rp, encoding="utf-8"))
        for rec in d["records"]:
            v = rec.get("value") or {}
            if v.get("status") != "form_processed":
                continue
            start = v.get("start_url") or ""
            landed = v.get("landed_url") or ""
            if not start or not landed or landed.rstrip("/") == start.rstrip("/"):
                continue
            observed[(start, landed)] += 1
            hops_seen[(start, landed)] = len(v.get("hops") or [])
    # Only a route that came back identical in two or more runs is a route; a URL that
    # differs per session (Refline/Workable/Prospective connector tokens) is a session,
    # and navigating to it cold cost 11 forms on 2026-08-29.
    for (start, landed), n in observed.items():
        if n < 2:
            continue
        host = (urlsplit(start).hostname or "").lower()
        routes_by_host[host].append({"from": start, "to": landed, "hops": hops_seen[(start, landed)], "seen_in_runs": n})
    n = 0
    for host, routes in routes_by_host.items():
        write_skill(root / "learned" / f"{slug(host)}.md", f"learned/{slug(host)}", f"learned routes for {host} ({len(routes)})",
                    [{"host": host}], {"routes": routes, "learned_from": [str(p) for p in results_paths]},
                    f"# learned: {host}\n\n{len(routes)} posting → application-view routes observed on 2026-08-29.")
        n += 1
    return {"learned_hosts": n, "routes": sum(len(r) for r in routes_by_host.values())}


def build_actions(root: Path, results_paths: list[Path]) -> dict:
    """The action cache: per host, the apply control (label + selector) whose click led
    to the form in a run, and per vendor the labels seen across the ATS map chains."""
    # learned per host from run records: the hop whose transition reached the form
    per_host: dict[str, Counter] = defaultdict(Counter)
    selectors: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for rp in results_paths:
        d = json.load(open(rp, encoding="utf-8"))
        for rec in d["records"]:
            v = rec.get("value") or {}
            if v.get("status") != "form_processed":
                continue
            hops = [h for h in (v.get("hops") or []) if isinstance(h, dict)]
            for i, h in enumerate(hops[:-1]):
                nxt = hops[i + 1]
                if not nxt.get("is_application"):
                    continue
                kind = (h.get("transition") or {}).get("kind")
                host = (urlsplit(h.get("url") or v.get("start_url") or "").hostname or "").lower()
                if kind in ("control", "new_target"):
                    ctl = h.get("apply_control") or {}
                    label = str(ctl.get("label") or "").strip()
                    if host and label:
                        per_host[host][label] += 1
                        if ctl.get("selector"):
                            selectors[(host, label)][ctl["selector"]] += 1
                elif kind in ("link", "fallback_link"):
                    label = str(h.get("apply_link_label") or "").strip()
                    if host and label:
                        per_host[host][label] += 1
                        if h.get("apply_link_selector"):
                            selectors[(host, label)][h["apply_link_selector"]] += 1
    n = 0
    for host, labels in per_host.items():
        actions = []
        for label, count in labels.most_common(3):
            sel = selectors[(host, label)].most_common(1)
            actions.append({"label": label, "selector": sel[0][0] if sel else None, "led_to_form": count})
        write_skill(root / "actions" / f"{slug(host)}.md", f"actions/{slug(host)}",
                    f"apply actions learned for {host}", [{"host": host}], {"actions": actions},
                    f"# actions: {host}\n\nControls whose click reached the application form, learned from runs on 2026-08-29.")
        n += 1
    # vendor-level labels from the ATS-map chains: the `chosen` apply candidate per vendor host
    vendor_labels: dict[str, Counter] = defaultdict(Counter)
    for line in open(ATS / "stage3_browser.jsonl", encoding="utf-8"):
        if not line.strip():
            continue
        rec = json.loads(line)
        for hop in rec.get("hops") or []:
            chosen = hop.get("chosen") or {}
            host = (urlsplit(hop.get("landed") or "").hostname or "").lower()
            vendor = vendor_for_host(host)
            if vendor and chosen.get("text"):
                vendor_labels[vendor][str(chosen["text"]).strip()[:60]] += 1
    m = 0
    for vendor, labels in vendor_labels.items():
        path = root / "ats" / f"{slug(vendor)}.md"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        block = json.loads(re.search(r"```json\n(\{.*?\})\n```", text, re.S).group(1))
        block["apply"]["actions"] = [{"label": label, "seen": count} for label, count in labels.most_common(3)]
        text = re.sub(r"```json\n\{.*?\}\n```", "```json\n" + json.dumps(block, ensure_ascii=False, indent=1) + "\n```", text, flags=re.S)
        path.write_text(text, encoding="utf-8")
        m += 1
    return {"action_hosts": n, "vendor_action_skills": m}


def write_sources(root: Path, with_learned: bool, with_actions: bool = False) -> None:
    rows = ([("ats", 60), ("company", 80)] + ([("learned", 100)] if with_learned else [])
            + ([("actions", 90)] if with_actions else []))
    text = "".join(f'[[source]]\nname = "{name}"\ntype = "path"\ntrust = "owner"\npriority = {prio}\npath = "{(root / name).as_posix()}"\n\n' for name, prio in rows)
    (root / "sources.toml").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "static"
    root = HERE / "skills" / which
    import shutil
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    report = build_static(root)
    if which == "cache":
        report.update(build_learned(root, [Path(p) / "results.json" for p in sys.argv[2:]]))
    if which == "actions":
        report.update(build_actions(root, [Path(p) / "results.json" for p in sys.argv[2:]]))
    write_sources(root, with_learned=(which == "cache"), with_actions=(which == "actions"))
    print(json.dumps({"corpus": str(root), **report}))
