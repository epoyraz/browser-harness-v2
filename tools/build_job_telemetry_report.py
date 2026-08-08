"""Build a self-contained HTML report from the 100-job dry-run artifacts."""
# ruff: noqa: ISC004 -- adjacent prose fragments keep the recommendations readable.
from __future__ import annotations

import html
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "job-form-telemetry-2026-08-08"
RESULTS = json.loads((OUT / "results.json").read_text(encoding="utf-8"))
JOBS_DOC = json.loads((ROOT / "jobs.json").read_text(encoding="utf-8"))
JOBS = {job["job_id"]: job for job in JOBS_DOC["jobs"]}
JOURNAL = [json.loads(line) for line in (OUT / "journal.jsonl").read_text(encoding="utf-8").splitlines() if line]
VALUES = [record["value"] for record in RESULTS["records"] if record.get("ok")]
FORMS = [value for value in VALUES if value.get("status") == "form_processed"]


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def pct(n: float, d: float) -> str:
    return f"{100 * n / d:.1f}%" if d else "—"


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(p * len(ordered)) - 1))
    return ordered[index]


def compact_reason(reason: str) -> str:
    if reason.startswith("page rendered nothing"):
        return "Empty DOM / bot wall / app boot failure"
    if reason.startswith("fewer than 2"):
        return "No substantial application form"
    if "none are usable" in reason:
        return "Collapsed or invisible controls"
    return reason or "No verdict"


calls = [entry for entry in JOURNAL if entry.get("kind") == "call"]
by_fn: dict[str, list[dict]] = defaultdict(list)
for call in calls:
    by_fn[call.get("fn", "unknown")].append(call)

call_stats = []
for fn, entries in by_fn.items():
    durations = [float(entry.get("ms") or 0) for entry in entries]
    call_stats.append({
        "fn": fn, "calls": len(entries),
        "failures": sum(not (entry.get("outcome") or {}).get("ok", False) for entry in entries),
        "total_ms": sum(durations), "p50": statistics.median(durations),
        "p95": percentile(durations, .95),
        "cdp": sum(int(entry.get("cdp") or 0) for entry in entries),
    })
call_stats.sort(key=lambda item: item["total_ms"], reverse=True)

# Chrome DevTools Protocol Monitor-inspired, value-free communication stream.
call_names = {entry.get("id"): entry.get("fn", "helper") for entry in calls}
explorer_events = []
for entry in JOURNAL:
    kind = entry.get("kind")
    if kind not in {"invoke", "call", "cdp"}:
        continue
    duration = float(entry.get("ms_total") if kind == "invoke" else entry.get("ms") or 0)
    ended = float(entry.get("ts") or 0) * 1000
    started = ended - duration
    if kind == "invoke":
        lane, name = "Model", "bh invocation"
        detail = {
            "lane": lane, "name": name, "duration_ms": duration,
            "source_lines": entry.get("source_lines"), "outcome": entry.get("outcome"),
            "note": "The journal records the invocation boundary, not prompt text or stdout.",
        }
        cdp_count = ""
        size = ""
        status = "ok" if entry.get("ok") else "failed"
    elif kind == "call":
        lane, name = "Harness", str(entry.get("fn") or "helper")
        outcome = entry.get("outcome") or {}
        detail = {
            "lane": lane, "name": name, "id": entry.get("id"),
            "parent": entry.get("parent"), "duration_ms": duration,
            "argument_keys": sorted((entry.get("args") or {}).keys()),
            "cdp_round_trips": entry.get("cdp", 0), "outcome": outcome,
        }
        cdp_count = entry.get("cdp", 0)
        size = ""
        status = "ok" if outcome.get("ok") else str(outcome.get("class") or "failed")
    else:
        lane, name = "CDP", str(entry.get("method") or "unknown")
        detail = {
            "lane": lane, "method": name, "id": entry.get("id"),
            "parent_helper": call_names.get(entry.get("parent")),
            "parent_id": entry.get("parent"), "duration_ms": duration,
            "request_bytes": entry.get("request_bytes", 0),
            "response_bytes": entry.get("response_bytes", 0),
            "parameter_keys": entry.get("param_keys") or [],
            "result_keys": entry.get("result_keys") or [],
            "ok": bool(entry.get("ok")), "error_class": entry.get("error_class"),
            "values_recorded": False,
        }
        cdp_count = 1
        size = f'{int(entry.get("request_bytes") or 0)}→{int(entry.get("response_bytes") or 0)} B'
        status = "ok" if entry.get("ok") else str(entry.get("error_class") or "failed")
    explorer_events.append({
        "started": started, "lane": lane, "name": name, "duration": duration,
        "cdp": cdp_count, "size": size, "status": status, "detail": detail,
    })
explorer_events.sort(key=lambda event: event["started"])
explorer_origin = min((event["started"] for event in explorer_events), default=0)
for event in explorer_events:
    event["offset"] = round(event.pop("started") - explorer_origin, 1)
explorer_json = json.dumps(explorer_events, ensure_ascii=False).replace("</", "<\\/")
explorer_rows = "".join(
    f'<tr data-event="{index}" data-lane="{esc(event["lane"])}">'
    f'<td>{event["offset"]/1000:.3f}s</td><td><span class="lane {event["lane"].lower()}">{esc(event["lane"])}</span></td>'
    f'<td><code>{esc(event["name"])}</code></td><td>{event["duration"]:.1f}ms</td>'
    f'<td>{esc(event["cdp"])}</td><td>{esc(event["size"])}</td><td>{esc(event["status"])}</td></tr>'
    for index, event in enumerate(explorer_events))

fields = [field for form in FORMS for field in form.get("field_audit", [])]
planned = sum(int(form.get("fill_plan_count") or 0) for form in FORMS)
filled = sum(int(((form.get("fill") or {}).get("observed") or {}).get("succeeded") or 0)
             for form in FORMS)
fill_failed = sum(int(((form.get("fill") or {}).get("observed") or {}).get("failed") or 0)
                  for form in FORMS)
uploads = [upload for form in FORMS for upload in form.get("uploads", [])]
upload_ok = sum(bool((upload.get("outcome") or {}).get("ok")) for upload in uploads)

missing_by_semantic: dict[str, dict] = {}
for form in FORMS:
    for field in form.get("field_audit", []):
        if field.get("status") != "missing_profile":
            continue
        sem = field["semantic"]
        row = missing_by_semantic.setdefault(
            sem, {"semantic": sem, "controls": 0, "required": 0, "jobs": set(), "examples": set()})
        row["controls"] += 1
        row["required"] += bool(field.get("required"))
        row["jobs"].add(form["job_id"])
        if field.get("label"):
            row["examples"].add(str(field["label"]).replace("\n", " "))
missing_rows = sorted(missing_by_semantic.values(), key=lambda row: (-len(row["jobs"]), -row["required"]))

unclassified = [field for field in fields if field.get("status") == "unclassified"]
required_unclassified = [field for field in unclassified if field.get("required")]
no_form_reasons = Counter(compact_reason((value.get("schema") or {}).get("verdict", {}).get("reason", ""))
                          for value in VALUES if value.get("status") == "no_application_form")

form_by_declared = {}
for mode in ("form", "account", "unknown"):
    group = [value for value in VALUES if value.get("declared_mode") == mode]
    form_by_declared[mode] = (sum(value.get("status") == "form_processed" for value in group), len(group))

job_wall = [float(value.get("wall_ms") or 0) for value in VALUES]
sum_job_ms = sum(job_wall)
wall_ms = float(RESULTS["meta"]["wall_ms"])
effective_parallelism = sum_job_ms / wall_ms if wall_ms else 0

fill_failure_classes = Counter()
for form in FORMS:
    for failure in ((form.get("fill") or {}).get("failures") or []):
        fill_failure_classes[failure.get("class") or "unknown"] += 1

recommendations = [
    ("Introduce one typed application workflow",
     f"The run was one model step but still orchestrated {len(calls)} helper calls. Add a general "
     "run_application(url, profile, policy) primitive that returns explicit NAVIGATED, FORM, "
     "ACCOUNT_REQUIRED, BOT_WALL, FILLED, and MISSING_INFO stages without submitting."),
    ("Resolve direct routes before opening Chrome",
     "This corpus originally sent 66 jobs through Joblens pages because only 34 had direct URLs. "
     "The MCP preflight now resolves 100/100 employer URLs, eliminating a whole navigation and "
     "discovery branch for those 66 items on the next run."),
    ("Keep state handling inside the application workflow",
     f"wait_for_application_state ran {len(by_fn.get('wait_for_application_state', []))} times and now distinguishes form, usable UI, account wall, bot wall, and stable failure. "
     "Keep this as one shared transition contract so future ATS shapes do not recreate load-event and target-switching races."),
    ("Make applicant profiles a first-class abstraction",
     f"The forms exposed {len(fields)} controls; a small ontology planned {planned}, while "
     f"{len(unclassified)} remained unclassified. A typed ApplicantProfile should carry value, "
     "source evidence, aliases, sensitivity, confidence, and an explicit unknown state."),
    ("Support semantic option candidates, not one literal label",
     f"{fill_failure_classes.get('no_option_match', 0)} fills failed because correct facts such "
     "as 8+ years or fluent English used different option wording. Let a plan supply ordered "
     "candidate labels and return candidates on ambiguity; never silently choose the first option."),
    ("Batch interactive widgets inside fill_form",
     f"fill_form used {sum(c['cdp'] for c in by_fn.get('fill_form', []))} CDP calls across "
     f"{len(by_fn.get('fill_form', []))} forms because trusted writes and widgets escape the one-shot "
     "batch. Extend the plan to native selects, comboboxes, masks, dates, and verification under "
     "one typed outcome."),
    ("Cache structural strategies, never live refs",
     f"prepare_application ran {len(by_fn.get('prepare_application', []))} times at "
     f"{(sum(c['cdp'] for c in by_fn.get('prepare_application', []))/max(len(by_fn.get('prepare_application', [])),1)):.1f} CDP calls each. Cache stable ATS/form capabilities and field semantics by "
     "structural fingerprint, while regenerating document-bound refs on every page."),
    ("Add a read-only network/API perception tier",
     f"{no_form_reasons.get('Empty DOM / bot wall / app boot failure', 0)} jobs produced an empty DOM. "
     "Where public JSON or HTML exists, obtain route and schema metadata without a renderer, then "
     "use the browser only for interaction. Keep this capability-based rather than host-specific."),
    ("Autotune concurrency against measured saturation",
     f"Ten tabs achieved {effective_parallelism:.2f} effective concurrent job-seconds with no worker "
     "or cleanup failures. Instead of raising the hard cap to 15 blindly, measure renderer memory, "
     "CDP queue latency, and host throttling, then select 6–10 workers dynamically per machine."),
    ("Make telemetry concurrency-aware and stage-aware",
     f"Summed helper time is {sum(s['total_ms'] for s in call_stats)/1000:.1f}s while wall time is "
     f"{wall_ms/1000:.1f}s, so serial percentage accounting is invalid. Record a per-item root span, "
     "critical path, queue time, stage outcome, and active-worker curve; classify wait_for_form as "
     "blocking so 'harness' does not absorb page waiting."),
]


def metric(label: str, value: str, note: str) -> str:
    return f'<article class="metric"><div class="metric-value">{esc(value)}</div><h3>{esc(label)}</h3><p>{esc(note)}</p></article>'


missing_labels = {
    "salary_expectation": "Salary expectation", "availability": "Start date / notice period",
    "linkedin_url": "LinkedIn URL", "github_url": "GitHub URL",
    "portfolio_url": "Portfolio / website", "tailored_response": "Tailored response",
    "referral_source": "Referral source", "gender_or_salutation": "Gender / salutation choice",
    "demographic": "Demographic choice", "consent": "Privacy consent choice",
}

missing_table = "".join(
    f"<tr><td>{esc(missing_labels.get(row['semantic'], row['semantic']))}</td>"
    f"<td>{len(row['jobs'])}</td><td>{row['controls']}</td><td>{row['required']}</td>"
    f"<td>{esc('; '.join(sorted(row['examples'])[:3]))}</td></tr>" for row in missing_rows)

call_table = "".join(
    f"<tr><td><code>{esc(row['fn'])}</code></td><td>{row['calls']}</td><td>{row['failures']}</td>"
    f"<td>{row['total_ms']/1000:.1f}s</td><td>{row['p50']:.0f}ms</td><td>{row['p95']:.0f}ms</td>"
    f"<td>{row['cdp']}</td><td>{row['cdp']/row['calls']:.1f}</td></tr>" for row in call_stats)

job_rows = []
raw_details = []
for value in sorted(VALUES, key=lambda item: item.get("rank") or 999):
    current = JOBS.get(value["job_id"], {})
    audit = value.get("field_audit") or []
    missing_required = sum(f.get("status") == "missing_profile" and f.get("required") for f in audit)
    unknown_required = sum(f.get("status") == "unclassified" and f.get("required") for f in audit)
    succeeded = int(((value.get("fill") or {}).get("observed") or {}).get("succeeded") or 0)
    failed = int(((value.get("fill") or {}).get("observed") or {}).get("failed") or 0)
    status_class = "good" if value.get("status") == "form_processed" else "muted"
    link = current.get("url") or value.get("landed_url") or ""
    job_rows.append(
        f'<tr data-status="{esc(value.get("status"))}"><td>{value.get("rank")}</td>'
        f'<td><a href="{esc(link)}">{esc(value.get("company"))}</a><small>{esc(value.get("title"))}</small></td>'
        f'<td>{esc(value.get("ats") or "unknown")}</td><td><span class="pill {status_class}">{esc(value.get("status"))}</span></td>'
        f'<td>{len(audit)}</td><td>{succeeded}/{succeeded+failed}</td><td>{missing_required}</td>'
        f'<td>{unknown_required}</td><td>{float(value.get("wall_ms") or 0)/1000:.1f}s</td></tr>')
    raw_details.append(
        f'<details><summary>#{value.get("rank")} {esc(value.get("company"))} — {esc(value.get("status"))}</summary>'
        f'<pre>{esc(json.dumps(value, indent=2, ensure_ascii=False))}</pre></details>')

recommendation_html = "".join(
    f'<article class="recommendation"><span>{i}</span><div><h3>{esc(title)}</h3><p>{esc(body)}</p></div></article>'
    for i, (title, body) in enumerate(recommendations, 1))

reason_html = "".join(
    f'<tr><td>{esc(reason)}</td><td>{count}</td></tr>' for reason, count in no_form_reasons.most_common())

route_html = "".join(
    f'<tr><td>{esc(mode)}</td><td>{found}</td><td>{total}</td><td>{pct(found,total)}</td></tr>'
    for mode, (found, total) in form_by_declared.items())

document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>browser-harness · 100-job telemetry</title>
<style>
:root{{--ink:#172126;--muted:#647178;--paper:#f5f1e8;--card:#fffdf8;--line:#d8d1c3;--accent:#176b5b;--warn:#a34f20;--blue:#2d5878}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 ui-sans-serif,system-ui,-apple-system,sans-serif}}
main{{max-width:1240px;margin:auto;padding:44px 24px 80px}} h1{{font:700 clamp(38px,7vw,76px)/.95 ui-serif,Georgia,serif;max-width:900px;margin:10px 0 18px}}
h2{{font:700 31px/1.1 ui-serif,Georgia,serif;margin:58px 0 18px}} h3{{margin:0 0 6px}} p{{color:var(--muted)}} .eyebrow{{text-transform:uppercase;letter-spacing:.16em;color:var(--accent);font-weight:800}}
.lede{{font-size:20px;max-width:850px}} .notice{{border-left:5px solid var(--accent);background:#e5f0eb;padding:14px 18px;margin:24px 0;border-radius:0 10px 10px 0}}
.metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:28px 0}} .metric{{background:var(--card);border:1px solid var(--line);padding:20px;border-radius:14px}}
.metric-value{{font:700 34px/1 ui-serif,Georgia,serif;color:var(--accent)}} .metric h3{{font-size:14px;margin-top:12px}} .metric p{{font-size:13px;margin:0}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:18px}} .panel{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px;overflow:auto}}
table{{width:100%;border-collapse:collapse}} th,td{{text-align:left;padding:10px 9px;border-bottom:1px solid var(--line);vertical-align:top}} th{{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}}
td small{{display:block;color:var(--muted);max-width:460px}} code{{background:#ece8df;padding:2px 5px;border-radius:4px}} a{{color:var(--blue)}}
.pill{{display:inline-block;padding:3px 8px;border-radius:99px;font-size:11px;font-weight:800}} .pill.good{{background:#dceee5;color:#155846}} .pill.muted{{background:#ebe7df;color:#695f55}}
.recommendation{{display:flex;gap:18px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin:10px 0}} .recommendation>span{{font:700 28px/1 ui-serif,Georgia,serif;color:var(--accent)}} .recommendation p{{margin:0}}
.links{{display:flex;gap:12px;flex-wrap:wrap}} .links a{{background:var(--ink);color:white;padding:9px 13px;border-radius:8px;text-decoration:none}}
details{{background:var(--card);border:1px solid var(--line);border-radius:8px;margin:8px 0;padding:10px}} summary{{cursor:pointer;font-weight:700}} pre{{white-space:pre-wrap;word-break:break-word;font-size:11px;max-height:600px;overflow:auto;background:#172126;color:#e9eee9;padding:14px;border-radius:8px}}
.filter{{padding:9px 12px;border:1px solid var(--line);border-radius:8px;background:white;margin-bottom:10px}} footer{{margin-top:64px;color:var(--muted)}}
.devtools{{display:grid;grid-template-columns:minmax(0,2fr) minmax(280px,1fr);padding:0;min-height:440px}} .protocol-list{{overflow:auto;max-height:560px;border-right:1px solid var(--line)}}
.protocol-list table{{font:12px/1.35 ui-monospace,SFMono-Regular,Menlo,monospace}} .protocol-list tr{{cursor:pointer}} .protocol-list tr.selected{{background:#dceee5}} .protocol-list td{{white-space:nowrap;padding:7px 8px}}
.detail-pane{{background:#172126;color:#e9eee9;padding:16px;overflow:auto}} .detail-pane h3{{font:600 13px/1.4 ui-sans-serif,system-ui;margin:0 0 10px}} .detail-pane pre{{margin:0;padding:0;max-height:none}}
.lane{{display:inline-block;min-width:52px;font:700 10px/1 ui-sans-serif,system-ui;text-transform:uppercase}} .lane.model{{color:#7b4d16}} .lane.harness{{color:var(--accent)}} .lane.cdp{{color:var(--blue)}}
@media(max-width:800px){{.metrics{{grid-template-columns:1fr 1fr}}.grid2{{grid-template-columns:1fr}}.devtools{{grid-template-columns:1fr}}.protocol-list{{border-right:0;border-bottom:1px solid var(--line);max-height:360px}}}} @media(max-width:480px){{.metrics{{grid-template-columns:1fr}}}}
</style></head><body><main>
<div class="eyebrow">browser-harness v2 · live dry run · 8 August 2026</div>
<h1>100 jobs, one Chrome, zero submissions.</h1>
<p class="lede">A ten-tab parallel pass inspected every job, followed application routes, filled only CV-supported facts, uploaded the CV where the input was unambiguous, and recorded unknown information instead of guessing.</p>
<div class="notice"><strong>Safety boundary:</strong> no applications were submitted. The harness blocked submit controls, form submission, Enter inside forms, and mutating requests. “Processed” means inspected and reversibly filled in browser state—not sent.</div>
<section class="metrics">
{metric('Jobs processed','100','100 worker records; no worker or cleanup failure')}
{metric('Wall time',f'{wall_ms/1000:.1f}s',f'{effective_parallelism:.2f} effective concurrent job-seconds across 10 tabs')}
{metric('Forms reached',f'{len(FORMS)}/100',f'{len(VALUES) - len(FORMS)} routes ended at account, bot-wall, listing, or no-form states')}
{metric('Verified fills',f'{filled}/{planned}',f'{pct(filled,planned)} of CV-backed planned fields; {fill_failed} typed failures')}
{metric('CV uploads',f'{upload_ok}/{len(uploads)}','Local file-control verification only; not server receipt')}
{metric('Fields observed',str(len(fields)),f'{sum(bool(f.get("required")) for f in fields)} inferred required controls')}
{metric('Missing profile controls',str(sum(r["controls"] for r in missing_rows)),f'{sum(r["required"] for r in missing_rows)} required; user choices are kept separate')}
{metric('Direct URLs',f'{JOBS_DOC["direct_url_resolution"]["resolved"]}/100','Resolved through Joblens MCP; jobs.json contains zero Joblens links')}
</section>

<h2>Reachability and route quality</h2><div class="grid2">
<section class="panel"><h3>Form discovery by declared route</h3><table><thead><tr><th>Route</th><th>Forms</th><th>Jobs</th><th>Rate</th></tr></thead><tbody>{route_html}</tbody></table></section>
<section class="panel"><h3>Why {len(VALUES) - len(FORMS)} jobs had no usable form</h3><table><thead><tr><th>Observed state</th><th>Jobs</th></tr></thead><tbody>{reason_html}</tbody></table></section>
</div>
<p>The distinction matters: all {len(VALUES)} parallel items completed successfully as harness work, but only {len(FORMS)} exposed a form the harness could honestly classify and fill.</p>

<h2>What is missing from the CV/profile</h2>
<section class="panel"><table><thead><tr><th>Information or choice</th><th>Jobs</th><th>Controls</th><th>Required</th><th>Examples</th></tr></thead><tbody>{missing_table}</tbody></table></section>
<p><strong>Interpretation:</strong> salary, start date, LinkedIn, GitHub, and portfolio are true reusable profile gaps. Consent, gender/salutation, demographics, and referral source are user choices or application context—not facts the harness should infer. There were also {len(required_unclassified)} required unclassified controls, dominated by bespoke questions and widget structures; these need a richer ontology or human input.</p>

<h2>Raw helper telemetry</h2>
<section class="panel"><table><thead><tr><th>Helper</th><th>Calls</th><th>Fail</th><th>Sum</th><th>p50</th><th>p95</th><th>CDP</th><th>CDP/call</th></tr></thead><tbody>{call_table}</tbody></table></section>
<p>Helper durations overlap across tabs. Their summed {sum(s['total_ms'] for s in call_stats)/1000:.1f}s is work-in-flight, not wall time. The current serial bench rollup incorrectly treats that sum as a percentage of one wall-clock critical path; this report keeps both quantities explicit.</p>

<h2>Communication Explorer</h2>
<p>A Chrome DevTools Protocol Monitor-style view of the Model boundary, harness helpers, and sanitized CDP round trips. Protocol values are never recorded; select a row to inspect its shape, parent helper, sizes, timing, and outcome.</p>
<input class="filter" id="protocol-filter" placeholder="Filter lane, helper, or CDP method…">
<section class="panel devtools"><div class="protocol-list"><table id="protocol-events"><thead><tr><th>Time</th><th>Lane</th><th>Name</th><th>Duration</th><th>CDP</th><th>Req→res</th><th>Status</th></tr></thead><tbody>{explorer_rows}</tbody></table></div><aside class="detail-pane"><h3>Request / response details</h3><pre id="protocol-detail">Select an event.</pre></aside></section>

<h2>10 general next steps</h2>{recommendation_html}

<h2>Every job</h2>
<input class="filter" id="filter" placeholder="Filter company, title, ATS, or status…">
<section class="panel"><table id="jobs"><thead><tr><th>#</th><th>Job</th><th>ATS</th><th>Outcome</th><th>Fields</th><th>Filled</th><th>Missing req.</th><th>Unknown req.</th><th>Wall</th></tr></thead><tbody>{''.join(job_rows)}</tbody></table></section>

<h2>Raw artifacts</h2><div class="links">
<a href="results.json">Input-ordered results.json</a><a href="results-completion-order.jsonl">Completion-order JSONL</a>
<a href="journal.jsonl">Harness journal</a><a href="trace.txt">Trace</a><a href="stats.txt">Stats</a>
<a href="bench.txt">Bench rollup</a><a href="joblens-mcp-details.jsonl">Raw MCP details</a><a href="../../jobs.json">Direct jobs.json</a>
</div>
<p>The embedded records below are the raw per-job browser results, including hops, schema verdicts, field audit, typed fill outcomes, upload evidence, and durations.</p>
<section>{''.join(raw_details)}</section>
<footer>Generated locally from the browser-harness journal and result artifacts. No external scripts, fonts, or analytics.</footer>
</main><script>
const filter=document.getElementById('filter'); filter.addEventListener('input',()=>{{const q=filter.value.toLowerCase();document.querySelectorAll('#jobs tbody tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q));}});
const protocolEvents={explorer_json};
const protocolFilter=document.getElementById('protocol-filter');
const protocolRows=[...document.querySelectorAll('#protocol-events tbody tr')];
const protocolDetail=document.getElementById('protocol-detail');
protocolFilter.addEventListener('input',()=>{{const q=protocolFilter.value.toLowerCase();protocolRows.forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q));}});
document.querySelector('#protocol-events tbody').addEventListener('click',event=>{{const row=event.target.closest('tr[data-event]');if(!row)return;document.querySelector('#protocol-events tr.selected')?.classList.remove('selected');row.classList.add('selected');const selected=protocolEvents[Number(row.dataset.event)];protocolDetail.textContent=JSON.stringify(selected.detail,null,2);}});
</script></body></html>"""

(OUT / "report.html").write_text(document, encoding="utf-8")
print(json.dumps({"report": str(OUT / "report.html"), "bytes": len(document.encode()),
                  "jobs": len(VALUES), "forms": len(FORMS), "filled": filled,
                  "planned": planned, "uploads_ok": upload_ok}, indent=2))
