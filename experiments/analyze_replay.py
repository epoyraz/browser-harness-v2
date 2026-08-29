"""Second application at the same employer: replay vs discovery, paired per posting.

    python analyze_replay.py out/replay/R-replay-1 out/replay/R-base-1 [more arms]
"""
from __future__ import annotations

import json
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path


def load(path: Path) -> dict:
    d = json.loads((path / "replay_results.json").read_text(encoding="utf-8"))
    entries = {e["job_id"]: e for e in d["entries"]}
    cdp = defaultdict(int)
    jp = path / "journal.jsonl"
    if jp.exists():
        for line in jp.open(encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("kind") == "call" and r.get("item_id"):
                cdp[r["item_id"]] += int(r.get("cdp") or 0)
    return {"name": path.name, "summary": d["summary"], "entries": entries, "cdp": cdp}


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(st.median(xs), 1) if xs else None


def main() -> None:
    arms = [load(Path(a)) for a in sys.argv[1:] if (Path(a) / "replay_results.json").exists()]
    for a in arms:
        s = a["summary"]
        print(f"{a['name']:12} wall={s['wall_ms']/1000:6.1f}s employers={s['employers']} postings={s['postings']} by_mode="
              + ", ".join(f"{m}: n={v['n']} ok={v['ok']} med={round((v['median_wall_ms'] or 0)/1000,1)}s" for m, v in s["by_mode"].items()))
    def is_replay(a):
        s = a["summary"]
        return bool(s.get("replay_enabled")) or s.get("mode") in ("auto", "record")
    replays = [a for a in arms if is_replay(a)]
    bases = [a for a in arms if not is_replay(a)]
    for r in replays:
        for b in bases:
            ids = [j for j, e in r["entries"].items() if e["mode"] in ("replay", "replay_fallback") and j in b["entries"]
                   and b["entries"][j]["mode"] == "discover"]
            rep_ok = [j for j in ids if r["entries"][j]["mode"] == "replay" and r["entries"][j].get("ok")]
            base_ok = [j for j in ids if b["entries"][j].get("ok")]
            both = [j for j in rep_ok if j in base_ok]
            d_ms = [float(r["entries"][j]["wall_ms"]) - float(b["entries"][j]["wall_ms"] or 0) for j in both]
            ratio = [float(b["entries"][j]["wall_ms"] or 0) / max(1.0, float(r["entries"][j]["wall_ms"])) for j in both]
            d_cdp = [r["cdp"].get(j, 0) - b["cdp"].get(j, 0) for j in both if r["cdp"] and b["cdp"]]
            fell = [j for j in ids if r["entries"][j]["mode"] == "replay_fallback"]
            lost = [j for j in ids if b["entries"][j].get("ok") and not r["entries"][j].get("ok")]
            gained = [j for j in ids if not b["entries"][j].get("ok") and r["entries"][j].get("ok")]
            print(f"PAIRED {r['name']} vs {b['name']}: second-or-later postings={len(ids)} replay_ok={len(rep_ok)} fell_back={len(fell)} "
                  f"forms: base={len(base_ok)} replay-arm={len(base_ok)-len(lost)+len(gained)} (lost {len(lost)}, gained {len(gained)})")
            if both:
                print(f"   on the {len(both)} postings both filled: replay median {med([r['entries'][j]['wall_ms'] for j in both])/1000:.1f}s vs "
                      f"discovery {med([b['entries'][j]['wall_ms'] for j in both])/1000:.1f}s; median delta {med(d_ms)/1000:+.1f}s; "
                      f"median speed-up {round(st.median(ratio),1)}x; CDP delta median {med(d_cdp)}")
            byco = Counter(r["entries"][j]["company"] for j in fell)
            if byco:
                print(f"   fallbacks by employer: {dict(byco)}")
            reasons = Counter(((r["entries"][j].get("replays") or [r["entries"][j].get("replay") or {}])[-1] or {}).get("reason")
                              for j in fell)
            if reasons:
                print(f"   fallback reasons: {dict(reasons)}")


if __name__ == "__main__":
    main()
