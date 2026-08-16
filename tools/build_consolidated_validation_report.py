"""Combine a full telemetry pass and ordered retry passes into one HTML report."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    latest: dict[str, tuple[dict, int]] = {}
    run_meta = []
    for pass_number, run_dir in enumerate(args.run, 1):
        payload = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
        run_meta.append({
            "pass": pass_number,
            "attempts": len(payload["records"]),
            "workers": payload["meta"]["workers_effective"],
            "wall_ms": payload["meta"]["wall_ms"],
            "submissions": payload["meta"]["submissions"],
            "report": f"../{run_dir.name}/report.html",
        })
        for record in payload["records"]:
            latest[str(record["item"]["job_id"])] = (record, pass_number)

    rows = []
    for record, pass_number in latest.values():
        item, value = record["item"], record.get("value") or {}
        errors = value.get("errors") or []
        rows.append({
            "rank": item.get("rank"),
            "company": item.get("company"),
            "title": item.get("title"),
            "url": item.get("url"),
            "site": item.get("employer_site"),
            "status": value.get("status") or record.get("class") or "unknown",
            "classification": value.get("form_classification") or "",
            "duration_ms": (record.get("telemetry") or {}).get("duration_ms"),
            "pass": pass_number,
            "error": (errors[0].get("class") if errors else ""),
        })
    rows.sort(key=lambda row: (row["rank"] is None, row["rank"] or 0))
    outcomes = Counter(row["status"] for row in rows)
    classifications = Counter(row["classification"] for row in rows if row["classification"])
    unresolved = outcomes["workflow_failed"] + outcomes["navigation_failed"]
    data = json.dumps({
        "rows": rows,
        "outcomes": outcomes,
        "classifications": classifications,
        "runs": run_meta,
        "unresolved": unresolved,
    }, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    document = TEMPLATE.replace("__DATA__", data)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(document, encoding="utf-8")
    print(json.dumps({"report": str(args.output.resolve()), "jobs": len(rows),
                      "outcomes": outcomes}, ensure_ascii=False))
    return 0


TEMPLATE = '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Consolidated job application validation</title><style>
:root{--bg:#f4f7fb;--ink:#17233b;--muted:#667085;--line:#dfe5ee;--blue:#2563eb}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,sans-serif}header{padding:42px max(24px,calc((100% - 1400px)/2));background:linear-gradient(125deg,#101a31,#244d68);color:#fff}h1{margin:0 0 8px;font-size:42px;letter-spacing:-.035em}header p{margin:0;color:#d4deea;font-size:16px}.wrap{max-width:1400px;margin:-20px auto 60px;padding:0 20px}.cards{display:grid;grid-template-columns:repeat(6,1fr);gap:12px}.card,.panel{background:#fff;border:1px solid var(--line);border-radius:14px;box-shadow:0 8px 28px #14213d12}.card{padding:18px}.card b{display:block;font-size:27px}.card span,.muted{color:var(--muted)}.panel{margin-top:18px;padding:20px}.runs{display:flex;gap:12px;flex-wrap:wrap}.runs a{color:var(--blue)}.toolbar{display:flex;gap:10px;margin:15px 0}.toolbar input,.toolbar select{padding:9px 11px;border:1px solid #ccd4df;border-radius:8px;background:#fff}.toolbar input{flex:1}table{width:100%;border-collapse:collapse}th,td{padding:10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{position:sticky;top:0;background:#f8fafc;font-size:11px;text-transform:uppercase}.table{max-height:70vh;overflow:auto;border:1px solid var(--line);border-radius:10px}.pill{padding:4px 8px;border-radius:999px;font-size:11px;font-weight:700;white-space:nowrap}.form_processed{background:#dcfce7;color:#087a55}.authentication_required{background:#dbeafe;color:#1d4ed8}.generic_form{background:#ede9fe;color:#6d28d9}.no_application_form{background:#fff3d6;color:#8a4c00}.workflow_failed,.navigation_failed{background:#ffe4e6;color:#9f1239}@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}}
</style></head><body><header><h1>Consolidated job validation</h1><p>Latest outcome per job after the 50-tab pass and ordered 20-tab and 10-tab retries.</p></header><main class="wrap"><section class="cards" id="cards"></section><section class="panel"><h2>Source passes</h2><div class="runs" id="runs"></div></section><section class="panel"><h2>All jobs</h2><p class="muted">Retry results replace only the corresponding earlier inconclusive result.</p><div class="toolbar"><input id="q" placeholder="Search company, job, site or classification"><select id="status"><option value="">All outcomes</option></select><span id="count"></span></div><div class="table"><table><thead><tr><th>#</th><th>Company</th><th>Job</th><th>Outcome</th><th>Classification</th><th>Pass</th><th>Time</th></tr></thead><tbody id="body"></tbody></table></div></section></main><script id="data" type="application/json">__DATA__</script><script>
const D=JSON.parse(document.getElementById('data').textContent),$=x=>document.getElementById(x),esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const O=D.outcomes,total=D.rows.length;const cards=[[total,'Jobs'],[O.form_processed||0,'Forms processed'],[O.authentication_required||0,'Authentication'],[O.generic_form||0,'Generic forms'],[O.no_application_form||0,'No application form'],[D.unresolved,'Unresolved']];$('cards').innerHTML=cards.map(x=>`<article class=card><b>${x[0].toLocaleString()}</b><span>${x[1]}</span></article>`).join('');$('runs').innerHTML=D.runs.map(r=>`<div><b>Pass ${r.pass}: ${r.workers} tabs</b> · ${r.attempts.toLocaleString()} attempts · ${(r.wall_ms/60000).toFixed(1)} min · <a href="${r.report}">details</a></div>`).join('');Object.keys(O).forEach(s=>$('status').insertAdjacentHTML('beforeend',`<option value="${s}">${s.replaceAll('_',' ')} (${O[s]})</option>`));function draw(){const q=$('q').value.toLowerCase(),s=$('status').value;const rows=D.rows.filter(r=>(!s||r.status===s)&&(!q||[r.company,r.title,r.site,r.classification].join(' ').toLowerCase().includes(q)));$('count').textContent=rows.length.toLocaleString()+' jobs';$('body').innerHTML=rows.map(r=>`<tr><td>${r.rank??''}</td><td><b>${esc(r.company)}</b><br><span class=muted>${esc(r.site)}</span></td><td><a href="${esc(r.url)}" target=_blank rel=noreferrer>${esc(r.title)}</a></td><td><span class="pill ${esc(r.status)}">${esc(r.status.replaceAll('_',' '))}</span></td><td>${esc(r.classification||r.error)}</td><td>${r.pass}</td><td>${r.duration_ms==null?'—':(r.duration_ms/1000).toFixed(1)+'s'}</td></tr>`).join('')}$('q').oninput=draw;$('status').onchange=draw;draw();
</script></body></html>'''


if __name__ == "__main__":
    raise SystemExit(main())
