"""Pair a replay run against its recording run: python analyze_pair.py <run1_dir> <run2_dir>"""
import json, sys
from collections import Counter, defaultdict
from statistics import median
def load(p):
    d = json.load(open(f"{p}/replay_results.json", encoding="utf-8")); return d["summary"], {e["job_id"]: e for e in d["entries"]}
s1, R1 = load(sys.argv[1]); s2, R2 = load(sys.argv[2])
for name, s, R in ((sys.argv[1].split('/')[-1], s1, R1), (sys.argv[2].split('/')[-1], s2, R2)):
    ent = list(R.values()); forms = sum(1 for e in ent if e.get("ok")); ni = sum(1 for e in ent if e["mode"] == "needs_input")
    ws = defaultdict(float); n = defaultdict(int)
    for e in ent: ws[e["mode"]] += e["wall_ms"]/1000; n[e["mode"]] += 1
    print(f"{name}: wall {round(s['wall_ms']/1000)}s phases {s.get('phase_walls_s')} forms {forms} (+{ni} needs_input) worker-s {round(sum(ws.values()))}",
          {m: (n[m], round(ws[m])) for m in ws})
second = [j for j in R2 if R2[j]["position"] > 0 and R2[j]["mode"] != "skipped"]
print("second-or-later postings", len(second), dict(Counter(R2[j]["mode"] for j in second)))
rep = [j for j in R2 if R2[j]["mode"] == "replay"]
if rep:
    print(f"replays {len(rep)}: median {round(median(R2[j]['wall_ms'] for j in rep)/1000,1)}s vs same postings in run1 {round(median(R1[j]['wall_ms'] for j in rep)/1000,1)}s;",
          "partial", sum(1 for j in rep if R2[j].get("partial")), "persona", dict(Counter((R2[j].get("persona_check") or {}).get("clean") for j in rep)))
fb = [j for j in R2 if R2[j]["mode"] == "replay_fallback"]
print("fallbacks", len(fb), Counter((R2[j]["company"], str((R2[j].get("replays") or [{}])[-1].get("reason"))[:45]) for j in fb).most_common(8))
lost = Counter((R2[j]["company"], R2[j]["mode"]) for j in R1 if R1[j].get("ok") and not R2[j].get("ok"))
gained = Counter((R2[j]["company"], R2[j]["mode"]) for j in R2 if R2[j].get("ok") and not R1[j].get("ok"))
print("lost", sum(lost.values()), dict(lost)); print("gained", sum(gained.values()), dict(gained))
