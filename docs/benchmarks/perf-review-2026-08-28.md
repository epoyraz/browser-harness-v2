# Performance review — where v2's time goes, and what would give it back

2026-08-28. Sources: the 100-posting telemetry run of 2026-08-21 (`post-review-100-rerun`,
10 workers, 106 s wall), the full CDP journal of the 2026-08-11 `no-upload-02` run (100
postings, 3,615 CDP calls, 902 helper spans), the four 50-tab runs of 2026-08-11, my
77-company probe of 2026-08-27 on current code, and an HTTP prescreen experiment run today
against the same 100 start URLs. Every number below is from one of those files.

**Verdict.** The fill is already fast. The harness spends **91% of every attempt navigating**,
and **46% of all attempt-seconds go to postings that end with no form** — most of which the
fetch metadata already identified before a tab was opened. The largest savings are steps
not taken, not steps taken faster. There are no LLM calls in the fill loop to remove; the
one LLM-side waste is already documented in the project's own benchmark and lives outside
the harness. `fetch-use` would not help: on this corpus a plain HTTP view of a posting is
wrong in both directions, and the only reliable cheap predictors need no fetch at all.

---

## 1. Where an attempt's time goes

100-run, 10 workers: wall 106 s, sum of attempt durations 1,026 s (pool ≈ 97% busy).
Mean attempt 10.3 s, median 7.6 s, p95 28.0 s, max 31.1 s.

| per-attempt phase | mean | p95 | share |
| --- | ---: | ---: | ---: |
| `navigate_ms` | 9.31 s | 28.9 s | **91%** |
| `fill_ms` (the 50 that reached a form) | 1.56 s | 5.7 s | 8% |

By outcome:

| outcome | n | mean | p95 | share of attempt-seconds | nav | hops |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `form_processed` | 50 | 9.9 s | 20.2 s | 48% | 8.2 s | 1.9 |
| `no_application_form` | 36 | 10.8 s | 30.1 s | **38%** | 10.6 s | 1.6 |
| `authentication_required` | 7 | 12.2 s | 31.1 s | **8%** | 12.0 s | 2.0 |
| `generic_form` | 6 | 7.6 s | 11.5 s | 4% | 7.5 s | 1.5 |

The p95/max of ~30 s on the dead-end rows is the 25 s locate timeout being reached: a
posting that yields nothing costs *more* than one that fills.

From the full CDP journal (08-11 code, same shape of work):

| CDP method | calls | ms total | share of CDP time |
| --- | ---: | ---: | ---: |
| `Page.navigate` | 136 | 101,122 | **75%** |
| `Runtime.evaluate` | 1,919 | 24,026 | 18% |
| `Page.getFrameTree` | 304 | 2,548 | 2% |
| everything else | ~1,250 | ~7,000 | 5% |

Helper span time by stage: navigate 352 s · transition 156 s · inspect 107 s · **fill 27 s**.
36 CDP round trips per posting; diagnostics (`Performance.*`, `Network.enable`…) are 6% of
round trips and <1% of time. The inner loop is not the problem.

---

## 2. Findings, ranked by what they would give back

### 2.1 Skip what the fetch already knew was dead — ~22% of compute, zero forms lost

`get_job_details` classifies every posting before any tab opens. Against the browser:

| `declared_mode` | n | browser outcome | attempt-seconds |
| --- | ---: | --- | ---: |
| `account` | 23 | **21 `no_application_form`**, 1 `generic_form`, 1 failed, **0 filled** | 228 |
| `form` | 16 | 14 filled, 2 no form | 98 |
| `unknown` | 61 | 36 filled, 13 no form, 7 auth wall, 5 generic | 700 |

By ATS: **Workday 17/17 `account_wall`. iCIMS 4/4 no form.** JazzHR, Lever, Ashby 3/3 filled
each. A dispatch rule *"skip `mode == account`; skip `*.myworkdayjobs.com` and iCIMS hosts"*
removes 228 of 1,026 attempt-seconds (22%) and loses no filled form. It is a string
comparison on data already in `jobs*.json`.

### 2.2 Remember the employer — 48% of attempts re-visit a company

100 attempts covered 52 companies. The 48 repeat visits cost 9.3 s each (first visits:
11.1 s) — no learning between attempts — and **19 of them re-hit the same dead end** as the
first visit (same Workday tenant, same wall). `attempt-timing.csv` already carries
`employer_site` and `employer_group_id` columns; nothing reads them. A per-site memo of
terminal outcomes (`thomsonreuters.wd5.myworkdayjobs.com → account_wall`, TTL a day) is
worth ~200 s on this corpus and compounds across runs.

### 2.3 Start at the form, not the posting — one whole hop per known ATS

40 of 50 filled forms needed exactly two hops: posting → apply view. For the big ATSs the
second URL is a function of the first:

```
Lever    jobs.lever.co/<co>/<id>          → …/<id>/apply
Ashby    jobs.ashbyhq.com/<co>/<id>       → …/<id>/application
```

`applications/document.py::_APPLICATION_ROUTE_RULES` already knows these — but
`follow_application` consults them only *after* landing on the posting, running
`prepare_document`, and waiting for application state. Starting at the candidate and
falling back to the posting only when the candidate is not a form saves one full
`goto` (3.5 s mean) + inspect + state wait per known-ATS posting. Cheaper still: compute
the form URL at fetch time, next to `ats` and `mode`, so the harness never sees the posting.

(Refline's second hop carries a token and is not derivable; adesso's second hop is a
same-site canonical redirect the harness pays for as a full hop.)

### 2.4 `wait_for_application_state` after every navigation — 14%

100 + 74 calls × ~0.8 s = 143 s in the journal, paid on landing *and* after each hop. On a
URL the route table recognises as a form view, the state is known at landing. The
navigation grace itself was already cut 3.0 → 0.8 s in `ee3cf0d` (−37% navigation,
−10.4% discovered fields — a measured trade-off, not a free win), so the remaining lever
here is skipping the wait where the answer is known, not shortening it further.

### 2.5 Workers: 10 is not the ceiling, 50 is past it

| workers | wall / attempt | attempt mean | attempt p95 | peak working set |
| ---: | ---: | ---: | ---: | ---: |
| 10 | 1.06 s | 10.3 s | 28 s | 25 GB |
| 50 | 0.64–0.77 s | **31–37 s** | 59–74 s | 30–43 GB |

Fifty tabs buy 1.5× throughput for 3× latency and near-total memory. Nothing between 10 and
50 has been run; the knee is somewhere in 15–25.

### 2.6 Already optimised — don't spend time here

The OOPIF probe gate (−13% round trips), the three round-trip classes in `33ed479`
(327 → 306 per three rounds), the navigation grace, and the usable-document re-ask are all
measured and landed. Diagnostics cost 6% of round trips and no time. The fill path is 8%
of an attempt and 96% accurate on writes.

---

## 3. LLM calls and steps

**There are none in the loop.** Every telemetry run reports `model_calls: 0`; the rules
planner fills every form. `run_application` is already the single big step (locate → hop →
prepare → plan → fill → verify in one call). `tools/model_planner.py` exists, sends field
*descriptions and answer names* rather than values, and caches by field digest so a re-run
is free — it has never been benchmarked on the corpus, so the rules-vs-model comparison it
was built for is still unmeasured.

The one LLM-side finding is already in `docs/benchmarks/application-decisions-2026-08-07.md`:
**80.8% of input tokens went to 37 model decisions that only polled process state** while a
long `bh` run was in flight. The bigger step is one blocking wait instead of a poll loop —
its effect dwarfs anything inside the harness, and the benchmark says so in as many words.

The remaining LLM-shaped work is deciding *what to attempt* — and §2.1 shows that decision
is largely a lookup.

---

## 4. Would `fetch-use` help? — No, and the corpus shows why

**What it is.** `fetch-use` (v1, `helpers.http_get`) is Browser Use's hosted HTTP proxy
(`fetch.browser-use.com`): a plain HTTP client with `proxy_country` routing, retries,
redirect following (`final_url`, `redirect_count`) and a cookie session. **It does not
render JavaScript.** v1 used it as an optional backend for pure-HTTP page reads; v2's
counterpart for bulk reads is in-page `fetch_all`, which rides the page's own cookies.

**The question it would answer:** can a cheap HTTP fetch classify a posting so the browser
only navigates to fillable ones? I measured that directly — one plain GET per start URL of
the 100-run, joined against what the browser actually found.

Cost: 100 URLs in **12.3 s** at 16 threads, median 610 ms each — versus 1,026 browser-seconds.
The speed is real. The signal is not:

| HTTP signal | what the browser found |
| --- | --- |
| redirect to a login URL | **0 of 100** — auth walls are JS-rendered, not HTTP redirects |
| `410 Gone` (jobs.sbb.ch, jobs.ruag.ch, jobs.usz.ch) | **4 of 5 were filled by the browser** — SuccessFactors answers 410 to non-browsers |
| `503` (coopjobs, helvetia, ksa) | 3 of 6 filled — a bot wall the real browser passes |
| `405` (iCIMS) | 4/4 no form — correct, but the *host* already says so |
| no `<form>` in HTML | true for **31 of 50 filled forms** — they are SPAs |
| `*.myworkdayjobs.com` in the URL | 17/17 no form — correct, and needs no fetch at all |

Prescreen rules scored against the browser:

| rule | skips | filled forms wrongly skipped | browser-seconds saved |
| --- | ---: | ---: | ---: |
| A: Workday host (URL only) | 17 | **0** | 87 |
| B: A + HTTP 405/410 | 26 | 4 | 274 |
| C: B + `200` without `<form>` | 55 | **19** | 573 |

Only rule A is safe, and rule A is a string match on the URL. Everything an HTTP body adds
is wrong on exactly the sites that matter: the ATSs return anti-bot statuses to plain
clients and render their forms from script.

**Where a proxy would matter:** 6 of 100 hosts returned 503/DataDome to a plain GET. The
browser filled 3 of them anyway. The 19 SSL failures in my run were Python's TLS stack on
this machine — `curl` fetched all 19 with `200` — not bot detection.

**So:** `fetch-use` would add a paid dependency and a second request per posting to gain a
signal the metadata already provides better. Its honest place in v2 would be as an optional
backend for a daemon-side bulk HTTP helper (the cross-origin case `fetch_all` cannot cover),
where anti-bot routing has value — not in the application loop, where every remaining
second is a real browser navigation to a page that needs a real browser.

---

## 5. What I would do, in order

1. **Dispatch filter from metadata** (§2.1). One function over `jobs*.json`: skip `account`
   mode and the account-only ATS hosts. Expected −22% attempt-seconds, −0 forms. An
   afternoon; verify by re-running the 100-posting telemetry and comparing forms filled.
2. **Form-URL rewrite at fetch time** (§2.3). Extend `_APPLICATION_ROUTE_RULES` use to the
   *start* URL, populated in the fetch alongside `ats`. Expected −1 hop (~4.5 s) on every
   Lever/Ashby/JazzHR-class posting; verify hops mean falls from 1.9 toward 1.0 on those.
3. **Employer-site memo** (§2.2). Keyed on `employer_site`, storing terminal outcome + TTL.
   Expected −19 dead-end repeats per 100 on this corpus; grows with corpus reuse.
4. **A 20-worker run** (§2.5) to find the knee between 1.06 s and 0.64 s per attempt.
5. **Skip the state wait on recognised form URLs** (§2.4). Smaller; do it after 2 makes
   most landings recognised.

Together 1–3 plausibly halve the 100-posting wall without touching the fill path or losing
a form. Each is independently measurable with the telemetry tool that already exists.

---

## 6. Caveats

- Cross-tabs are from one 100-run; the direction is robust (Workday 17/17, `account` 21/23)
  but the percentages will move with the corpus.
- The CDP journal is from 08-11 code; the phase *shape* still holds (my 08-27 probe on
  current code: filled 5.7 s mean, dead ends 4–12 s) but absolute numbers are older.
- The HTTP prescreen ran from this Windows machine; the SSL failures are local. `fetch-use`
  itself was not exercised — no API key — so its own latency and cost are unmeasured. That
  does not change the conclusion, which rests on what an HTTP body can and cannot say about
  these sites.
