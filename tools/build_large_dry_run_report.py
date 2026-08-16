"""Build a self-contained HTML dashboard for the large application dry run."""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUN = ROOT / "outputs" / "job-form-telemetry-2026-08-11-full-50tabs"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    args = parse_args()
    run_dir = args.run_dir.resolve()
    output = (args.output or run_dir / "report.html").resolve()
    results = load_json(run_dir / "results.json")
    timing = load_json(run_dir / "timing-report.json")
    memory = load_json(run_dir / "chrome-memory-summary.json")
    memory_samples = [
        json.loads(line)
        for line in (run_dir / "chrome-memory-samples.jsonl").read_text(
            encoding="utf-8").splitlines()
        if line.strip() and "working_set_bytes" in line
    ]

    records = results["records"]
    statuses = Counter(
        (record.get("value") or {}).get("status") or record.get("class") or "unknown"
        for record in records
    )
    record_by_job = {
        str((record.get("item") or {}).get("job_id")): record for record in records
    }
    attempts = []
    for attempt in timing["attempts"]:
        row = dict(attempt)
        source = (record_by_job.get(str(row.get("job_id"))) or {}).get("item") or {}
        row["url"] = source.get("url")
        row["company_aliases"] = source.get("company_aliases") or row.get("company_aliases")
        attempts.append(row)

    samples = [{
        "seconds": round(float(sample.get("offset_ms") or 0) / 1000, 1),
        "private_gib": round(float(sample.get("private_bytes") or 0) / 2**30, 3),
        "working_gib": round(float(sample.get("working_set_bytes") or 0) / 2**30, 3),
        "private_delta_gib": round(float(sample.get("private_delta_bytes") or 0) / 2**30, 3),
        "active": int(sample.get("active_attempts") or 0),
        "pages": int(sample.get("page_targets") or 0),
    } for sample in memory_samples]

    data = {
        "generated": results["meta"]["generated"],
        "meta": results["meta"],
        "parallel": results["parallel_summary"],
        "whole_timing": timing["whole_set"],
        "outcomes": dict(statuses),
        "employer_groups": len({
            (record.get("item") or {}).get("employer_group_id") for record in records
        }),
        "memory": memory,
        "memory_samples": samples,
        "companies": timing["companies"],
        "attempts": attempts,
        "artifacts": {
            "results": "results.json",
            "timing": "timing-report.json",
            "company_csv": "company-timing.csv",
            "attempt_csv": "attempt-timing.csv",
            "memory": "chrome-memory-summary.json",
            "memory_samples": "chrome-memory-samples.jsonl",
        },
    }
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    document = HTML.replace("__REPORT_DATA__", encoded)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(document.encode("utf-8"))
    print(json.dumps({
        "report": str(output),
        "bytes": output.stat().st_size,
        "attempts": len(attempts),
        "companies": len(timing["companies"]),
        "memory_samples": len(samples),
    }, ensure_ascii=False))
    return 0


HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>50-tab employer application dry run</title>
<style>
:root{--ink:#17233b;--muted:#667085;--paper:#f5f7fb;--panel:#fff;--line:#e4e8f0;--navy:#14213d;--green:#0e9f6e;--blue:#2563eb;--amber:#d97706;--red:#dc2626;--purple:#7c3aed;--shadow:0 12px 34px rgba(20,33,61,.08)}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}
.hero{background:linear-gradient(125deg,#101a31 0%,#1f315d 58%,#17465a 100%);color:#fff;padding:54px 0 74px;position:relative;overflow:hidden}.hero:after{content:"";position:absolute;width:440px;height:440px;border-radius:50%;right:-120px;top:-220px;background:rgba(72,187,155,.16);pointer-events:none;z-index:0}.hero .wrap{position:relative;z-index:1}.wrap{width:min(1440px,calc(100% - 40px));margin:auto}.eyebrow{text-transform:uppercase;letter-spacing:.16em;font-weight:750;font-size:12px;color:#7ce0bd}.hero h1{font-size:clamp(34px,5vw,62px);line-height:1.02;letter-spacing:-.045em;margin:12px 0 16px;max-width:900px}.hero p{font-size:17px;color:#d4dbea;max-width:760px;margin:0}.hero-meta{display:flex;gap:20px;flex-wrap:wrap;margin-top:26px;color:#aebbd0;font-size:13px}.nav{display:flex;gap:10px;flex-wrap:wrap;margin-top:25px}.nav a{color:#fff;border:1px solid rgba(255,255,255,.2);padding:8px 12px;border-radius:999px;background:rgba(255,255,255,.06)}
main{margin-top:-38px;position:relative;padding-bottom:64px}.cards{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:14px}.card,.panel{background:var(--panel);border:1px solid rgba(228,232,240,.95);border-radius:16px;box-shadow:var(--shadow)}.card{padding:19px}.card .value{font-size:28px;line-height:1.1;font-weight:780;letter-spacing:-.035em}.card .label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;margin-top:7px}.card .note{color:var(--muted);font-size:12px;margin-top:5px}.grid{display:grid;grid-template-columns:1.15fr .85fr;gap:16px;margin-top:16px}.panel{padding:22px;min-width:0}.panel h2{font-size:20px;letter-spacing:-.02em;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 18px}.section-title{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;margin:34px 0 12px}.section-title h2{margin:0;font-size:25px;letter-spacing:-.03em}.section-title p{margin:0;color:var(--muted)}
.outcome-stack{display:flex;height:22px;border-radius:999px;overflow:hidden;background:#edf0f5;margin:18px 0}.outcome-stack span{min-width:2px;transition:width .3s}.legend{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.legend-row{display:flex;align-items:center;justify-content:space-between;border:1px solid var(--line);border-radius:11px;padding:10px 12px}.legend-name{display:flex;align-items:center;gap:9px}.dot{width:10px;height:10px;border-radius:50%}.legend strong{font-variant-numeric:tabular-nums}
.chart{height:300px;width:100%;display:block}.chart.small{height:230px}.chart-key{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin-top:8px}.chart-key span:before{content:"";display:inline-block;width:18px;height:3px;border-radius:2px;margin-right:7px;vertical-align:middle;background:var(--key)}
.ram-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-top:16px}.ram-stat{border:1px solid var(--line);border-radius:12px;padding:13px}.ram-stat b{font-size:21px;display:block}.ram-stat span{color:var(--muted);font-size:12px}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:13px}.toolbar input,.toolbar select{border:1px solid #ccd3df;border-radius:9px;background:#fff;padding:9px 11px;color:var(--ink);font:inherit}.toolbar input{min-width:260px;flex:1}.count{color:var(--muted);margin-left:auto}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:12px}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px 11px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:11px;text-transform:uppercase;letter-spacing:.075em;color:var(--muted);background:#f8fafc;position:sticky;top:0;z-index:1;white-space:nowrap}td{font-variant-numeric:tabular-nums}tr:last-child td{border-bottom:0}.company{font-weight:680}.job-title{min-width:260px}.status{display:inline-flex;border-radius:999px;padding:4px 8px;font-size:11px;font-weight:700;white-space:nowrap}.status-form_processed{color:#087a55;background:#dcfce7}.status-no_application_form{color:#8a4c00;background:#fff3d6}.status-authentication_required{color:#1d4ed8;background:#dbeafe}.status-generic_form{color:#7c3aed;background:#ede9fe}.status-workflow_failed{color:#9f1239;background:#ffe4e6}.status-navigation_failed{color:#5b21b6;background:#ede9fe}.status-unknown{color:#475467;background:#eef1f5}.pagination{display:flex;align-items:center;justify-content:flex-end;gap:10px;margin-top:12px}.pagination button{border:1px solid #ccd3df;background:#fff;border-radius:8px;padding:7px 11px;cursor:pointer}.pagination button:disabled{opacity:.45;cursor:default}
.method{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.method article{border:1px solid var(--line);border-radius:12px;padding:15px}.method h3{margin:0 0 7px;font-size:15px}.method p{margin:0;color:var(--muted)}.downloads{display:flex;gap:10px;flex-wrap:wrap}.download{border:1px solid var(--line);border-radius:10px;padding:9px 12px;background:#fff}.footer{color:var(--muted);text-align:center;margin-top:28px;font-size:12px}
@media(max-width:1100px){.cards{grid-template-columns:repeat(3,1fr)}.grid{grid-template-columns:1fr}.ram-grid{grid-template-columns:repeat(2,1fr)}}@media(max-width:680px){.wrap{width:min(100% - 22px,1440px)}.hero{padding-top:38px}.cards{grid-template-columns:repeat(2,1fr)}.ram-grid,.method,.legend{grid-template-columns:1fr}.card .value{font-size:23px}.panel{padding:16px}.toolbar input{min-width:100%}.count{margin-left:0}}
</style>
</head>
<body>
<header class="hero"><div class="wrap">
  <div class="eyebrow">Browser Harness · measured production corpus</div>
  <h1 id="report-title">Employer application dry run</h1>
  <p>Every direct employer link in the expanded Joblens-derived test set, processed under a non-submitting browser boundary with per-attempt timing and continuous Chrome memory sampling.</p>
  <div class="hero-meta"><span id="generated"></span><span>Zero submissions</span><span>Self-contained report</span></div>
  <nav class="nav"><a href="#overview">Overview</a><a href="#memory">Memory</a><a href="#companies">Companies</a><a href="#attempts">Attempts</a><a href="#method">Method</a></nav>
</div></header>
<main class="wrap">
  <section class="cards" id="overview"></section>
  <section class="grid">
    <article class="panel"><h2>Observed outcomes</h2><p class="sub">A typed result was retained for every URL; “completed” does not mean a form existed.</p><div class="outcome-stack" id="outcome-stack"></div><div class="legend" id="outcome-legend"></div></article>
    <article class="panel"><h2>Attempt duration</h2><p class="sub" id="attempt-summary"></p><canvas class="chart small" id="duration-chart"></canvas></article>
  </section>

  <div class="section-title" id="memory"><div><h2>Chrome memory under load</h2><p>Connected Chrome instance sampled every two seconds.</p></div></div>
  <section class="panel"><canvas class="chart" id="memory-chart"></canvas><div class="chart-key"><span style="--key:#2563eb">Private memory</span><span style="--key:#7c3aed">Summed working set</span><span style="--key:#0e9f6e">Active attempts</span></div><div class="ram-grid" id="ram-grid"></div></section>

  <div class="section-title" id="companies"><div><h2>Timing by company</h2><p id="company-summary"></p></div><a href="company-timing.csv">Download CSV</a></div>
  <section class="panel"><div class="toolbar"><input id="company-search" type="search" placeholder="Filter company…"><select id="company-sort"><option value="mean">Slowest average</option><option value="sum">Largest total</option><option value="name">Company A–Z</option><option value="attempts">Most attempts</option></select><span class="count" id="company-count"></span></div><div class="table-wrap"><table><thead><tr><th>Company</th><th>Attempts</th><th>Outcome mix</th><th>Mean</th><th>Median</th><th>P95</th><th>Max</th><th>Sum</th></tr></thead><tbody id="company-body"></tbody></table></div></section>

  <div class="section-title" id="attempts"><div><h2>Every attempt</h2><p>Direct employer URL, typed outcome, worker, duration and tab heap.</p></div><a href="attempt-timing.csv">Download CSV</a></div>
  <section class="panel"><div class="toolbar"><input id="attempt-search" type="search" placeholder="Search company, title, site or job ID…"><select id="status-filter"><option value="">All outcomes</option></select><select id="attempt-sort"><option value="rank">Input order</option><option value="slow">Slowest first</option><option value="fast">Fastest first</option><option value="company">Company A–Z</option></select><span class="count" id="attempt-count"></span></div><div class="table-wrap"><table><thead><tr><th>#</th><th>Company</th><th>Job</th><th>Outcome</th><th>Duration</th><th>Worker</th><th>JS heap</th></tr></thead><tbody id="attempt-body"></tbody></table></div><div class="pagination"><button id="prev">Previous</button><span id="page-label"></span><button id="next">Next</button></div></section>

  <div class="section-title" id="method"><div><h2>Method and artifacts</h2><p>How to interpret the measurements.</p></div></div>
  <section class="panel"><div class="method"><article><h3>Safety boundary</h3><p>The workflow has no submission operation. Submit controls, form submission, beacons and application POSTs were blocked; the recorded submission count is zero.</p></article><article><h3>RAM per tab</h3><p id="ram-method"></p></article><article><h3>Outcome semantics</h3><p><strong>form_processed</strong> is reserved for structurally relevant application forms. Authentication-only and generic/contact forms are reported separately instead of being inferred from a low field count.</p></article></div><h3>Download raw artifacts</h3><div class="downloads" id="downloads"></div></section>
  <div class="footer">Generated entirely from local dry-run artifacts. No external scripts, fonts or analytics.</div>
</main>
<script id="report-data" type="application/json">__REPORT_DATA__</script>
<script>
const D=JSON.parse(document.getElementById('report-data').textContent);
const $=id=>document.getElementById(id);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const sec=ms=>ms==null?'—':(ms/1000).toFixed(ms>=100000?1:2)+'s';
const mib=b=>b==null?'—':(b/1048576).toFixed(1)+' MiB';
const gib=b=>b==null?'—':(b/1073741824).toFixed(2)+' GiB';
const pct=(n,d)=>d?(100*n/d).toFixed(1)+'%':'—';
const duration=D.whole_timing.attempt_duration_ms, mem=D.memory, worker=mem.incremental_private_per_worker_tab_bytes;
const workers=D.meta.workers_effective||D.meta.worker_limit||1;
document.title=workers+'-tab employer application dry run';
$('report-title').textContent=workers+'-tab employer application dry run';
$('attempt-summary').textContent='Distribution across all '+D.whole_timing.attempts.toLocaleString()+' attempts.';
$('company-summary').textContent=D.companies.length.toLocaleString()+' source-company aliases, including shared portal aliases.';
$('ram-method').textContent='OS-level tab RAM is an incremental average over steady '+workers+'-worker samples. Chrome can share one process across tabs or assign several renderers to one tab, so exact attribution is not possible.';
$('generated').textContent='Completed '+D.generated.replace('T',' ');
const cards=[
  [D.whole_timing.attempts.toLocaleString(),'Attempts','100% timed'],
  [D.companies.length.toLocaleString(),'Company aliases',D.employer_groups+' employer portals'],
  [(D.whole_timing.wall_ms/60000).toFixed(1)+' min','Whole-set wall',workers+' reusable tabs'],
  [sec(duration.median),'Median attempt','P95 '+sec(duration.p95)],
  [gib(mem.peak_private_bytes),'Peak private RAM','+'+gib(mem.peak_private_delta_bytes)+' vs baseline'],
  [mib(worker.mean),'Mean RAM / tab','P95 '+mib(worker.p95)]
];
$('overview').innerHTML=cards.map(x=>`<article class="card"><div class="value">${esc(x[0])}</div><div class="label">${esc(x[1])}</div><div class="note">${esc(x[2])}</div></article>`).join('');
const palette={form_processed:'#0e9f6e',authentication_required:'#2563eb',generic_form:'#7c3aed',no_application_form:'#d97706',workflow_failed:'#dc2626',navigation_failed:'#7c3aed',unknown:'#667085'};
const labels={form_processed:'Form processed',authentication_required:'Authentication required',generic_form:'Generic form',no_application_form:'No application form',workflow_failed:'Workflow failed',navigation_failed:'Navigation failed',unknown:'Unknown'};
const total=Object.values(D.outcomes).reduce((a,b)=>a+b,0);
$('outcome-stack').innerHTML=Object.entries(D.outcomes).map(([k,v])=>`<span title="${esc(labels[k]||k)}: ${v}" style="width:${100*v/total}%;background:${palette[k]||palette.unknown}"></span>`).join('');
$('outcome-legend').innerHTML=Object.entries(D.outcomes).map(([k,v])=>`<div class="legend-row"><span class="legend-name"><i class="dot" style="background:${palette[k]||palette.unknown}"></i>${esc(labels[k]||k)}</span><strong>${v.toLocaleString()} · ${pct(v,total)}</strong></div>`).join('');
function setupCanvas(canvas,height){const ratio=devicePixelRatio||1,w=canvas.clientWidth,h=height;canvas.width=w*ratio;canvas.height=h*ratio;const c=canvas.getContext('2d');c.setTransform(ratio,0,0,ratio,0,0);return {c,w,h};}
function axes(c,w,h,left,bottom,maxY,yLabel){c.strokeStyle='#dfe4ec';c.lineWidth=1;c.beginPath();c.moveTo(left,12);c.lineTo(left,h-bottom);c.lineTo(w-10,h-bottom);c.stroke();c.fillStyle='#7b8495';c.font='11px system-ui';for(let i=0;i<=4;i++){const y=12+(h-bottom-12)*i/4,val=maxY*(1-i/4);c.fillText(val.toFixed(maxY>100?0:1)+' '+yLabel,4,y+4);c.strokeStyle='#eef1f5';c.beginPath();c.moveTo(left,y);c.lineTo(w-10,y);c.stroke();}}
function drawMemory(){const canvas=$('memory-chart'),{c,w,h}=setupCanvas(canvas,300),left=48,bottom=26,s=D.memory_samples;if(!s.length)return;const maxY=Math.max(...s.map(x=>x.working_gib))*1.05,maxX=s[s.length-1].seconds;axes(c,w,h,left,bottom,maxY,'GiB');const plot=(key,color)=>{c.strokeStyle=color;c.lineWidth=2;c.beginPath();s.forEach((x,i)=>{const px=left+(w-left-10)*(x.seconds/maxX),py=12+(h-bottom-12)*(1-x[key]/maxY);i?c.lineTo(px,py):c.moveTo(px,py)});c.stroke()};plot('working_gib','#7c3aed');plot('private_gib','#2563eb');c.fillStyle='rgba(14,159,110,.12)';c.beginPath();s.forEach((x,i)=>{const px=left+(w-left-10)*(x.seconds/maxX),py=h-bottom-(h-bottom-12)*(x.active/workers);i?c.lineTo(px,py):c.moveTo(px,py)});c.lineTo(w-10,h-bottom);c.lineTo(left,h-bottom);c.closePath();c.fill();c.fillStyle='#7b8495';c.fillText('0',left-2,h-7);c.fillText((maxX/60).toFixed(1)+' min',w-48,h-7)}
function drawHistogram(){const vals=D.attempts.map(x=>Number(x.duration_ms||0)/1000),bins=14,max=Math.max(...vals),counts=Array(bins).fill(0);vals.forEach(v=>counts[Math.min(bins-1,Math.floor(v/max*bins))]++);const canvas=$('duration-chart'),{c,w,h}=setupCanvas(canvas,230),left=40,bottom=26,maxC=Math.max(...counts);axes(c,w,h,left,bottom,maxC,'');const bw=(w-left-10)/bins;c.fillStyle='#2563eb';counts.forEach((v,i)=>{const bh=(h-bottom-12)*v/maxC;c.fillRect(left+i*bw+2,h-bottom-bh,Math.max(2,bw-4),bh)});c.fillStyle='#7b8495';c.fillText('0s',left,h-7);c.fillText(max.toFixed(0)+'s',w-34,h-7)}
$('ram-grid').innerHTML=[['Baseline private',gib(mem.baseline.private_bytes)],['Peak private',gib(mem.peak_private_bytes)],['Peak increase',gib(mem.peak_private_delta_bytes)],['Peak working set',gib(mem.peak_working_set_bytes)],['Per-tab median',mib(worker.median)],['Per-tab P95',mib(worker.p95)],['Tab JS heap median',mib(mem.per_attempt_js_heap_used_bytes.median)],['Steady samples',mem.steady_full_pool_samples.toLocaleString()]].map(x=>`<div class="ram-stat"><b>${esc(x[1])}</b><span>${esc(x[0])}</span></div>`).join('');
function statusMix(x){return Object.entries(x||{}).map(([k,v])=>`${labels[k]||k}: ${v}`).join(' · ')}
function renderCompanies(){const q=$('company-search').value.trim().toLowerCase(),sort=$('company-sort').value;let rows=D.companies.filter(x=>x.company.toLowerCase().includes(q));rows.sort((a,b)=>sort==='name'?a.company.localeCompare(b.company):sort==='sum'?b.sum_attempt_duration_ms-a.sum_attempt_duration_ms:sort==='attempts'?b.attempts-a.attempts:(b.attempt_duration_ms.mean||0)-(a.attempt_duration_ms.mean||0));$('company-count').textContent=rows.length.toLocaleString()+' companies';$('company-body').innerHTML=rows.map(x=>`<tr><td class="company">${esc(x.company)}</td><td>${x.attempts}</td><td>${esc(statusMix(x.statuses))}</td><td>${sec(x.attempt_duration_ms.mean)}</td><td>${sec(x.attempt_duration_ms.median)}</td><td>${sec(x.attempt_duration_ms.p95)}</td><td>${sec(x.attempt_duration_ms.max)}</td><td>${sec(x.sum_attempt_duration_ms)}</td></tr>`).join('')}
let page=1;const pageSize=75;
function attemptRows(){const q=$('attempt-search').value.trim().toLowerCase(),status=$('status-filter').value,sort=$('attempt-sort').value;let rows=D.attempts.filter(x=>(!status||x.status===status)&&(!q||[x.company,x.title,x.employer_site,x.job_id].join(' ').toLowerCase().includes(q)));rows.sort((a,b)=>sort==='slow'?b.duration_ms-a.duration_ms:sort==='fast'?a.duration_ms-b.duration_ms:sort==='company'?a.company.localeCompare(b.company):a.rank-b.rank);return rows}
function renderAttempts(){const rows=attemptRows(),pages=Math.max(1,Math.ceil(rows.length/pageSize));page=Math.min(page,pages);const shown=rows.slice((page-1)*pageSize,page*pageSize);$('attempt-count').textContent=rows.length.toLocaleString()+' attempts';$('attempt-body').innerHTML=shown.map(x=>`<tr><td>${x.rank}</td><td><span class="company">${esc(x.company)}</span><br><span class="sub">${esc(x.employer_site||'')}</span></td><td class="job-title"><a href="${esc(x.url||'#')}" target="_blank" rel="noreferrer">${esc(x.title)}</a><br><span class="sub">${esc(x.job_id)}</span></td><td><span class="status status-${esc(x.status||'unknown')}">${esc(labels[x.status]||x.status||'unknown')}</span></td><td>${sec(x.duration_ms)}</td><td>${x.worker_id??'—'}</td><td>${mib(x.js_heap_used_bytes)}</td></tr>`).join('');$('page-label').textContent=`Page ${page} of ${pages}`;$('prev').disabled=page<=1;$('next').disabled=page>=pages}
Object.keys(D.outcomes).forEach(k=>$('status-filter').insertAdjacentHTML('beforeend',`<option value="${esc(k)}">${esc(labels[k]||k)} (${D.outcomes[k]})</option>`));
$('company-search').addEventListener('input',renderCompanies);$('company-sort').addEventListener('change',renderCompanies);['attempt-search','status-filter','attempt-sort'].forEach(id=>$(id).addEventListener(id==='attempt-search'?'input':'change',()=>{page=1;renderAttempts()}));$('prev').onclick=()=>{page--;renderAttempts()};$('next').onclick=()=>{page++;renderAttempts()};
$('downloads').innerHTML=Object.entries(D.artifacts).map(([k,v])=>`<a class="download" href="${esc(v)}">${esc(k.replaceAll('_',' '))}</a>`).join('');
renderCompanies();renderAttempts();drawMemory();drawHistogram();let resizeTimer;addEventListener('resize',()=>{clearTimeout(resizeTimer);resizeTimer=setTimeout(()=>{drawMemory();drawHistogram()},120)});
</script>
</body></html>'''


if __name__ == "__main__":
    raise SystemExit(main())
