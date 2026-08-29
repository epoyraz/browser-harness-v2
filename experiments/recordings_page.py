"""Render the recordings library as one HTML page (same design system as the ATS map)."""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
ATS = json.load(open(HERE.parent / "ats-map" / "ats_map_final.json", encoding="utf-8"))
ATS_BY_COMPANY = {r["company"]: r for r in ATS}
ATS_BY_HOST = {}
for r in ATS:
    h = (urlsplit(r.get("final_application_url") or "").hostname or "").lower()
    if h and h not in ATS_BY_HOST:
        ATS_BY_HOST[h] = r


def load(root: Path) -> list[dict]:
    recs = []
    for p in sorted(root.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for rec in (data if isinstance(data, list) else [data]):
            if isinstance(rec, dict) and rec.get("host"):
                recs.append(rec)
    return recs


def main() -> None:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else HERE / "recordings")
    out = Path(sys.argv[2] if len(sys.argv) > 2 else HERE / "recordings.html")
    recs = load(root)
    rows = []
    for rec in recs:
        meta = ATS_BY_COMPANY.get(rec.get("company")) or ATS_BY_HOST.get(rec["host"]) or {}
        fields = rec.get("fields") or []
        gaps = [g for g in (rec.get("required_unplanned") or []) if isinstance(g, dict)]
        stats = rec.get("stats") or {}
        rows.append({
            "company": rec.get("company") or "", "host": rec["host"], "ats": meta.get("ats_family") or "",
            "apply_type": meta.get("application_type") or "", "jobs": meta.get("jobs_on_joblens") or 0,
            "steps": [{"action": s.get("action"), "label": s.get("label"), "selector": s.get("selector")} for s in rec.get("steps") or []],
            "fields": [{"label": f.get("label"), "semantic": f.get("semantic"), "kind": f.get("kind"),
                        "selector": f.get("selector"), "required": bool(f.get("required"))} for f in fields],
            "gaps": [{"label": g.get("label"), "semantic": g.get("semantic")} for g in gaps],
            "n_fields": len(fields), "n_required": sum(1 for f in fields if f.get("required")),
            "n_gaps": len(gaps), "fingerprint": rec.get("fingerprint"), "recorded_from": rec.get("recorded_from"),
            "form_url": rec.get("form_url"), "recorded_at": rec.get("recorded_at"),
            "uses": stats.get("uses", 0), "successes": stats.get("successes", 0),
            "retired": stats.get("consecutive_failures", 0) >= 2,
        })
    rows.sort(key=lambda r: (-r["jobs"], r["company"]))
    sem = Counter(f["semantic"] or "unclassified" for r in rows for f in r["fields"])
    gaps = Counter(g["semantic"] or "unclassified" for r in rows for g in r["gaps"])
    kpis = {"recordings": len(rows), "hosts": len({r["host"] for r in rows}), "employers": len({r["company"] for r in rows}),
            "one_click": sum(1 for r in rows if r["steps"]), "direct": sum(1 for r in rows if not r["steps"]),
            "fields_median": sorted(r["n_fields"] for r in rows)[len(rows) // 2] if rows else 0,
            "with_gaps": sum(1 for r in rows if r["gaps"]), "jobs_covered": sum(r["jobs"] for r in rows),
            "ats": Counter(r["ats"] or "custom / unknown" for r in rows).most_common(12),
            "semantics": sem.most_common(14), "gaps": gaps.most_common(10)}
    data = json.dumps({"rows": rows, "kpis": kpis, "library": str(root), "generated": rows[0]["recorded_at"] if rows else ""},
                      ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.replace("__DATA__", data)
    out.write_text(html, encoding="utf-8")
    print("wrote", out, len(html), "bytes;", kpis["recordings"], "recordings", kpis["hosts"], "hosts")


TEMPLATE = r"""<title>joblens Replay Library</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
  :root { color-scheme: light; --ground:#f3f6f4; --surface:#fff; --surface-2:#eaf0ed; --line:#d7dfdb; --line-strong:#b9c4bf; --ink:#172120; --ink-2:#4f5b58; --ink-3:#7d8884; --accent:#2a78d6; --accent-ink:#1c5cab; --accent-soft:#dbe8f8; --ok:#1baf7a; --warn:#eb6834; --focus:#eb6834; --shadow:0 1px 2px rgba(23,33,32,.06),0 8px 24px -12px rgba(23,33,32,.18); }
  @media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { color-scheme: dark; --ground:#131716; --surface:#1b201f; --surface-2:#242b29; --line:#2d3532; --line-strong:#3f4a46; --ink:#f1f4f2; --ink-2:#b4bdb9; --ink-3:#7f8a86; --accent:#3987e5; --accent-ink:#86b6ef; --accent-soft:#1d2e44; --ok:#199e70; --warn:#d95926; --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px -12px rgba(0,0,0,.6);} }
  :root[data-theme="dark"] { color-scheme: dark; --ground:#131716; --surface:#1b201f; --surface-2:#242b29; --line:#2d3532; --line-strong:#3f4a46; --ink:#f1f4f2; --ink-2:#b4bdb9; --ink-3:#7f8a86; --accent:#3987e5; --accent-ink:#86b6ef; --accent-soft:#1d2e44; --ok:#199e70; --warn:#d95926; --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px -12px rgba(0,0,0,.6); }
  * { box-sizing:border-box } body { margin:0; background:var(--ground); color:var(--ink); font:15px/1.5 "IBM Plex Sans","Segoe UI",system-ui,sans-serif }
  a { color:var(--accent-ink) } .wrap { max-width:1240px; margin:0 auto; padding:0 28px 72px }
  h1,h2,h3 { font-family:"Bricolage Grotesque","IBM Plex Sans",sans-serif; text-wrap:balance; margin:0 } .num { font-family:"IBM Plex Mono",ui-monospace,Consolas,monospace; font-variant-numeric:tabular-nums }
  .mast { padding:44px 0 26px; border-bottom:1px solid var(--line-strong); display:grid; grid-template-columns:1fr auto; gap:24px; align-items:end }
  .eyebrow { font-size:12px; letter-spacing:.08em; text-transform:uppercase; color:var(--ink-3); margin-bottom:10px } h1 { font-size:clamp(34px,5vw,52px); font-weight:700; line-height:1.02; letter-spacing:-.01em }
  .lede { max-width:64ch; color:var(--ink-2); margin:14px 0 0; font-size:16px } .meta { color:var(--ink-3); font-size:13px; text-align:right; line-height:1.7 } .meta b { color:var(--ink-2); font-weight:500 }
  .kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:14px; margin:26px 0 36px } .kpi { background:var(--surface); border:1px solid var(--line); border-radius:6px; padding:16px 18px 14px; box-shadow:var(--shadow) }
  .kpi .label { font-size:12px; letter-spacing:.06em; text-transform:uppercase; color:var(--ink-3) } .kpi .value { font-family:"Bricolage Grotesque",sans-serif; font-size:40px; font-weight:700; line-height:1.05; margin-top:8px; font-variant-numeric:tabular-nums } .kpi .sub { color:var(--ink-2); font-size:13px; margin-top:6px }
  section { margin-top:40px } .sec-head { display:flex; align-items:baseline; justify-content:space-between; gap:16px; flex-wrap:wrap; margin-bottom:14px } h2 { font-size:24px; font-weight:700 } .sec-head p { margin:4px 0 0; color:var(--ink-2); max-width:70ch }
  .card { background:var(--surface); border:1px solid var(--line); border-radius:6px; padding:20px 22px; box-shadow:var(--shadow) } .two { display:grid; grid-template-columns:1fr 1fr; gap:18px } @media (max-width:900px) { .two { grid-template-columns:1fr } .mast { grid-template-columns:1fr } .meta { text-align:left } }
  .bars { display:grid; grid-template-columns:max-content minmax(0,1fr) max-content; column-gap:12px; row-gap:7px; align-items:center } .bars .name { font-size:14px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:230px } .bars .track { height:16px; position:relative } .bars .fill { position:absolute; left:0; top:0; height:100%; background:var(--accent); border-radius:0 4px 4px 0; min-width:2px } .bars .fill.warn { background:var(--warn) } .bars .val { font-size:13px; color:var(--ink-2) }
  .controls { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:12px } .controls input, .controls select { font:inherit; font-size:14px; padding:7px 10px; border:1px solid var(--line-strong); border-radius:4px; background:var(--surface); color:var(--ink) } .controls input { min-width:260px } .count { margin-left:auto; color:var(--ink-3); font-size:13px }
  .tbl-wrap { overflow-x:auto; border:1px solid var(--line); border-radius:6px; background:var(--surface) } table { border-collapse:collapse; width:100%; font-size:13.5px } th,td { text-align:left; padding:9px 12px; border-bottom:1px solid var(--line); vertical-align:top }
  th { position:sticky; top:0; background:var(--surface-2); font-size:12px; letter-spacing:.05em; text-transform:uppercase; color:var(--ink-2); font-weight:600; white-space:nowrap } td.r,th.r { text-align:right } tr.main { cursor:pointer } tr.main:hover td { background:color-mix(in srgb,var(--accent-soft) 45%,transparent) }
  tr.detail td { background:var(--surface-2); color:var(--ink-2); font-size:12.5px } .prog { display:grid; grid-template-columns:120px 1fr; gap:6px 14px; align-items:start } .prog b { color:var(--ink); font-weight:600 } .step { display:inline-block; padding:2px 8px; border-radius:999px; background:var(--accent-soft); color:var(--accent-ink); font-size:12px; margin:2px 4px 2px 0 }
  .fld { display:inline-flex; gap:6px; align-items:center; padding:2px 8px; border:1px solid var(--line); border-radius:4px; margin:2px 4px 2px 0; font-size:12px; background:var(--surface) } .fld .sem { color:var(--accent-ink); font-family:"IBM Plex Mono",monospace; font-size:11px } .fld.req { border-color:var(--line-strong) } .gap { color:var(--warn) }
  .pill { display:inline-block; font-size:11.5px; padding:2px 8px; border-radius:999px; background:var(--surface-2); color:var(--ink-2) } .pill.ok { background:color-mix(in srgb,var(--ok) 18%,transparent); color:var(--ink) } .pill.warn { background:color-mix(in srgb,var(--warn) 18%,transparent); color:var(--ink) }
  .sel { font-family:"IBM Plex Mono",monospace; font-size:11px; color:var(--ink-3) } .more { text-align:center; padding:12px } .toggle { background:none; border:1px solid var(--line-strong); color:var(--ink-2); font:inherit; font-size:13px; padding:6px 12px; border-radius:4px; cursor:pointer }
  footer { margin-top:48px; color:var(--ink-3); font-size:12.5px; border-top:1px solid var(--line); padding-top:14px }
</style>
<div class="wrap">
  <header class="mast">
    <div>
      <div class="eyebrow">Swiss employers · recorded application programs</div>
      <h1>joblens Replay Library</h1>
      <p class="lede">One discovery per employer, recorded as a replayable program: the apply control, every form field with its meaning, and the required fields no profile could answer. Values are never stored — each replay plans them for whoever is applying.</p>
    </div>
    <div class="meta">recorded <b id="m-date"></b><br>library <b id="m-lib"></b><br>persona used to record: <b>Max Mustermann</b> (values discarded)</div>
  </header>
  <div class="kpis" id="kpis"></div>
  <section><div class="two">
    <div class="card"><h3 style="font-size:16px;margin-bottom:10px">What the recorded forms ask for</h3><div class="bars" id="sem"></div></div>
    <div class="card"><h3 style="font-size:16px;margin-bottom:10px">Required fields discovery could not plan</h3><div class="bars" id="gaps"></div><p style="margin:12px 0 0;color:var(--ink-3);font-size:12.5px">These are what the preflight reports per applicant before any navigation.</p></div>
  </div></section>
  <section>
    <div class="sec-head"><div><h2>Recorded employers</h2><p>Click a row for the program. Sorted by open jobs on joblens.</p></div></div>
    <div class="controls"><input type="search" id="q" placeholder="Search employer, host, ATS, field…" aria-label="Search"><select id="ats"><option value="">All systems</option></select><select id="kind"><option value="">All programs</option><option value="click">One click then form</option><option value="direct">Form on the landing page</option><option value="gaps">Has unanswerable required fields</option></select><span class="count" id="count"></span></div>
    <div class="tbl-wrap"><table id="tbl"><thead><tr><th>Employer</th><th>Host</th><th>System</th><th class="r">Jobs</th><th>Program</th><th class="r">Fields</th><th class="r">Required</th><th class="r">Gaps</th><th>Replays</th></tr></thead><tbody id="tbody"></tbody><div class="more" id="more"></div></table></div>
  </section>
  <footer>Built from <span class="num">experiments/recordings/&lt;host&gt;.json</span> by <span class="num">recordings_page.py</span> · recorder/replayer: <span class="num">v2/applications/replay.py</span>.</footer>
</div>
<script id="data" type="application/json">__DATA__</script>
<script>
(() => {
  const D = JSON.parse(document.getElementById('data').textContent); const K = D.kpis; const rows = D.rows;
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const fmt = n => Number(n).toLocaleString('de-CH');
  document.getElementById('m-date').textContent = (D.generated || '').slice(0, 10); document.getElementById('m-lib').textContent = D.library.replace(/\\/g, '/').split('/').slice(-2).join('/');
  document.getElementById('kpis').innerHTML = [
    ['Recordings', K.recordings, `${K.hosts} employer hosts · ${K.employers} employers`],
    ['One-click programs', K.one_click, `${K.direct} forms sit on the landing page`],
    ['Fields per form', K.fields_median, 'median recorded fields'],
    ['Open jobs covered', fmt(K.jobs_covered), 'on joblens, at the recorded employers'],
    ['Forms with gaps', K.with_gaps, 'have required fields no profile answers'],
  ].map(([l, v, s]) => `<div class="kpi"><div class="label">${l}</div><div class="value">${v}</div><div class="sub">${s}</div></div>`).join('');
  const bars = (id, pairs, warn) => { const max = Math.max(...pairs.map(p => p[1])); document.getElementById(id).innerHTML = pairs.map(([k, v]) => `<div class="name" title="${esc(k)}">${esc(k)}</div><div class="track"><div class="fill${warn ? ' warn' : ''}" style="width:${(v / max * 100).toFixed(1)}%"></div></div><div class="val num">${v}</div>`).join(''); };
  bars('sem', K.semantics, false); bars('gaps', K.gaps, true);
  const atsSel = document.getElementById('ats'); K.ats.forEach(([a, n]) => { const o = document.createElement('option'); o.value = a; o.textContent = `${a} (${n})`; atsSel.appendChild(o); });
  const state = { q: '', ats: '', kind: '', limit: 100 };
  const filtered = () => rows.filter(r => (!state.ats || (r.ats || 'custom / unknown') === state.ats) && (!state.kind || (state.kind === 'click' ? r.steps.length > 0 : state.kind === 'direct' ? r.steps.length === 0 : r.gaps.length > 0)) && (!state.q || [r.company, r.host, r.ats, r.apply_type, ...r.fields.map(f => f.label + ' ' + (f.semantic || '')), ...r.steps.map(s => s.label)].join(' ').toLowerCase().includes(state.q.toLowerCase())));
  const render = () => { const out = filtered(); const shown = out.slice(0, state.limit); document.getElementById('count').textContent = `${out.length} of ${rows.length} recordings`;
    document.getElementById('tbody').innerHTML = shown.map((r, i) => `<tr class="main" data-i="${rows.indexOf(r)}"><td><b>${esc(r.company)}</b><br><small style="color:var(--ink-3)">${esc(r.apply_type)}</small></td><td class="sel">${esc(r.host)}</td><td>${esc(r.ats || 'custom / unknown')}</td><td class="r num">${fmt(r.jobs)}</td><td>${r.steps.length ? r.steps.map(s => `<span class="step">${esc(s.label || s.action)}</span>`).join('') : '<span class="pill">form on landing page</span>'}</td><td class="r num">${r.n_fields}</td><td class="r num">${r.n_required}</td><td class="r num">${r.n_gaps ? `<span class="gap">${r.n_gaps}</span>` : '0'}</td><td>${r.retired ? '<span class="pill warn">retired</span>' : r.uses ? `<span class="pill ok">${r.successes}/${r.uses} ok</span>` : '<span class="pill">not replayed yet</span>'}</td></tr>`).join('');
    document.getElementById('more').innerHTML = out.length > shown.length ? `<button class="toggle" id="more-btn">Show ${Math.min(100, out.length - shown.length)} more</button>` : ''; };
  render();
  document.getElementById('q').addEventListener('input', e => { state.q = e.target.value; state.limit = 100; render(); });
  atsSel.addEventListener('change', e => { state.ats = e.target.value; state.limit = 100; render(); });
  document.getElementById('kind').addEventListener('change', e => { state.kind = e.target.value; state.limit = 100; render(); });
  document.getElementById('more').addEventListener('click', e => { if (e.target.id === 'more-btn') { state.limit += 100; render(); } });
  document.getElementById('tbody').addEventListener('click', e => { const tr = e.target.closest('tr.main'); if (!tr) return; const next = tr.nextElementSibling; if (next && next.classList.contains('detail')) { next.remove(); return; } const r = rows[+tr.dataset.i]; const d = document.createElement('tr'); d.className = 'detail';
    d.innerHTML = `<td colspan="9"><div class="prog"><b>Recorded from</b><span>${esc(r.recorded_from)} → <a href="${esc(r.form_url)}" target="_blank" rel="noopener">${esc((r.form_url || '').slice(0, 90))}</a> · fingerprint <span class="sel">${esc(r.fingerprint)}</span></span><b>Steps</b><span>${r.steps.length ? r.steps.map(s => `<span class="step">${esc(s.action)}: ${esc(s.label || '')}</span> <span class="sel">${esc(s.selector || 'by label')}</span>`).join('<br>') : 'none — the landing page is the form'}</span><b>Fields (${r.n_fields})</b><span>${r.fields.map(f => `<span class="fld${f.required ? ' req' : ''}" title="${esc(f.selector || '')}">${esc(f.label || '(no label)')} <span class="sem">${esc(f.semantic || f.kind || '')}</span>${f.required ? ' *' : ''}</span>`).join('')}</span><b>Needs input</b><span>${r.gaps.length ? r.gaps.map(g => `<span class="fld gap">${esc(g.label || '(no label)')} <span class="sem">${esc(g.semantic || 'unclassified')}</span></span>`).join('') : '<span class="pill ok">every required field is answerable from a profile</span>'}</span></div></td>`; tr.after(d); });
})();
</script>
"""

if __name__ == "__main__":
    main()
