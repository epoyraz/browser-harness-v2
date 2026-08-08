"""Render one MP4 per captured application and an animated 5x5 HTML grid."""
from __future__ import annotations

import html
import json
import shutil
from pathlib import Path

from harness.ops.video import export

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "top-25-applications-2026-08-08"
MANIFEST = OUT / "manifest.json"


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def relative(path: str | Path) -> str:
    return Path(path).resolve().relative_to(OUT.resolve()).as_posix()


payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
records = [record for record in payload["records"] if record.get("ok")]
values = [record["value"] for record in records]

for value in values:
    recording = ROOT / value["recording"]
    rendered = export(recording, overwrite=True)
    value["video"] = relative(rendered["path"])
    value["recording_frames"] = rendered["shots"]
    value["recording_seconds"] = rendered["duration"]

excluded = sorted(values, key=lambda value: (value["recording_seconds"], value["rank"]))[:5]
excluded_ids = {value["job_id"] for value in excluded}
for value in excluded:
    (ROOT / value["screenshot"]).unlink(missing_ok=True)
    shutil.rmtree(ROOT / value["recording"], ignore_errors=True)

values = sorted(
    (value for value in values if value["job_id"] not in excluded_ids),
    key=lambda value: value["rank"],
)
payload["meta"].update({
    "captured": 30,
    "selected": 25,
    "exclusion": "five shortest rendered recordings",
})
payload["excluded_shortest"] = [
    {
        "rank": value["rank"],
        "job_id": value["job_id"],
        "company": value["company"],
        "recording_seconds": value["recording_seconds"],
    }
    for value in excluded
]
payload["records"] = [
    record for record in records if record["value"]["job_id"] not in excluded_ids
]
payload["summary"].update({
    "worker_ok": 25,
    "screenshots": 25,
    "fresh_clean": sum(not value.get("errors") for value in values),
    "required_missing": sum(int(value.get("missing_required") or 0) for value in values),
    "required_unknown": sum(int(value.get("unknown_required") or 0) for value in values),
    "videos": 25,
})
MANIFEST.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

cards = []
for value in values:
    screenshot = relative(ROOT / value["screenshot"])
    cards.append(f"""
<article class="card">
  <video controls preload="metadata" poster="{esc(screenshot)}">
    <source src="{esc(value['video'])}" type="video/mp4">
  </video>
  <div class="body">
    <div class="rank">#{value['rank']} · {esc(value['company'])}</div>
    <h2>{esc(value['title'])}</h2>
    <div class="facts"><span>{value.get('recording_frames', 0)} frames</span><span>{value.get('recording_seconds', 0):.1f}s</span></div>
    <div class="facts warning"><span>{value.get('missing_required', 0)} missing required</span><span>{value.get('unknown_required', 0)} unknown required</span></div>
    <a href="{esc(screenshot)}">Screenshot</a>
  </div>
</article>""")

document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Top 25 application recordings</title>
<style>
:root{{--ink:#172126;--muted:#68747a;--paper:#f4f0e7;--card:#fff;--line:#d7d0c4;--accent:#176b5b;--warn:#9b491e}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:14px/1.4 system-ui,sans-serif;overflow-x:hidden}}
main{{max-width:1800px;margin:auto;padding:28px}}header{{display:flex;align-items:end;justify-content:space-between;gap:24px;margin-bottom:22px}}
h1{{font:700 clamp(32px,5vw,66px)/.95 Georgia,serif;margin:0}}header p{{max-width:650px;color:var(--muted)}}
.stage{{transform-origin:top left;transition:transform 2.2s cubic-bezier(.2,.8,.2,1)}}.grid{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px}}.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;min-width:0;transition:opacity .7s ease}}
video{{display:block;width:100%;aspect-ratio:16/10;background:#162025;object-fit:cover}}.body{{padding:12px}}.rank{{font-weight:800;color:var(--accent)}}
h2{{font-size:14px;line-height:1.3;min-height:2.6em;margin:5px 0 10px}}.facts{{display:flex;gap:8px;flex-wrap:wrap;color:var(--muted);font-size:12px}}.warning{{color:var(--warn);margin:3px 0 9px}}
a{{color:var(--accent)}}body.intro{{overflow:hidden}}body.intro header{{opacity:0;height:0;margin:0;overflow:hidden}}body.intro .stage{{transform:scale(5.08)}}body.intro .card:not(:first-child){{opacity:0}}header{{transition:opacity .5s ease}}
@media(max-width:1200px){{.grid{{grid-template-columns:repeat(5,minmax(210px,1fr));min-width:1120px}}body.intro .stage{{transform:scale(4.4)}}}}@media(max-width:700px){{header{{display:block}}}}
</style></head><body><main>
<header><div><div>browser-harness · dry run</div><h1>25 application recordings</h1></div><p>One isolated recording per application in a complete 5×5 grid. The view begins on the first application, then zooms out to reveal the set. No files were uploaded and no applications were submitted.</p></header>
<div class="stage"><section class="grid">{''.join(cards)}</section></div>
</main><script>
document.body.classList.add('intro');
window.scrollTo(0,0);
setTimeout(()=>document.body.classList.remove('intro'),1200);
</script></body></html>"""

(OUT / "grid.html").write_text(document, encoding="utf-8")
print(json.dumps({"grid": str(OUT / "grid.html"), "applications": len(values), "videos": payload["summary"]["videos"], "layout": "5x5"}))
