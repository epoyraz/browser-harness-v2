# 24 theories, one corpus: what the 100-posting run accepts and rejects

2026-08-29, 15:30–19:40 local. 55 arms of `tools/collect_job_form_telemetry.py` over
`jobs_run100.json` (100 fresh joblens postings, 20 skipped by the dispatch filter → 80
attempted per arm), plus 3 pilots and two browser-only experiments. Every arm: a fresh
scratch Chrome (headless unless stated), a fresh `exp` daemon spawned from the current
code, 10 workers, `BH_CDP_TRACE=1`, dry run. Runner and analyzer live in
`../experiments/` (`run_arm.py`, `analyze.py`, `schedule.json`); per-arm artifacts under
`../experiments/out/<exp>/<arm>/`.

## Protocol

The rules from `corpus-noise-2026-08-29.md`, applied mechanically:

* Every treatment is one env switch on the same binary, run twice (`r1` in the first half
  of the schedule, `r2` in the second half in reverse order), each paired **per posting**
  against the adjacent controls (17 headless, 3 headed). The tables report gained/lost
  forms and the sum/median of per-posting attempt-second deltas on the matched 80.
* A verdict needs both replicates to agree in direction, or a unanimous answer.
  Opposite signs mean "no robust effect", never "pick the better run".
* Controls this evening: forms 44–50 (one 37), wall 68–165 s. The machine was shared
  (the user's Chrome, Spotify, Discord) and DNS resolved at 0.9–1.4 s per lookup from
  ~17:00, so absolute seconds are the weather; paired deltas are the signal.

## Before any experiment could run: the daemon was killing its client

Three pilots died mid-run. The journal said why: `peer_evicted`. The daemon fanned
**every** CDP event to a subscribed client and evicted it when its queue passed 2,048
frames — 265 MB of `Network.responseReceivedExtraInfo` in 41 s (pilot 1), then 521 MB of
`Runtime.consoleAPICalled` (pilot 2), 986 MB of `Log.entryAdded` (pilot 3), 2.7 GB of
`Network.requestWillBeSent` at 23 KB each (E01-r1). Client code reads about fourteen
event methods and a handful of fields.

Fix, in three parts, each measured:

| change | measurement |
| --- | --- |
| client names its events at `subscribe`; daemon forwards only those (`harness/connect/client.py: EVENT_FILTER`, `daemon.py: _Peer.methods`) | filtered frames 38–60k per run |
| Network events slimmed to `requestId/loaderId/frameId/type/…`, `request.url/method`, `response.status/mimeType/url` (`daemon.py: _slim_event`) | bytes to client **1.34 GB (E25, filter off) → 17–25 MB** per run |
| frame cap 2,048 → 32,768 (byte ceiling stays the memory bound) | teardown burst of 107k × 500 B frames no longer evicts |

Result: 0 evictions in 55 arms. Peak queue depth 882 (unfiltered) → 74–103. This is almost
certainly the mid-run `browser_disconnected` that stopped the 500-employer ATS-map run at
item ~225 the same morning. `BH_EVENT_FILTER=0` restores the old contract; a `peer_gone`
journal event now distinguishes "client closed" from a real eviction.

## Verdicts

### Accepted

| # | theory | evidence (paired, both replicates) | change |
| --- | --- | --- | --- |
| E00 | event filter + slimming + frame cap | above | default |
| E02 | `challenge.kind`: only interstitials are `detected` | ATS-map records: of 204 hops the old flag marked, 113 were real application/login pages, 1 a real wall | default |
| E07 | cleanup accounting | 268 of 500 records in the ATS-map pass 1 were `resource_cleanup_failed` with their `value` present | journal `peer_gone`; records keep `value` |
| E08 | popup quiet window 200 → 50 ms | cleanup 20.9 → 6.2 s per run (both replicates); forms +3/−0, +0/−1, +5/−0, +2/−3; 0 late popups in 320 items, all 4 descendants seen were caught | default 50 ms; `cleanup_descendants` is the tripwire |
| E11 | `usable_after=None` for the application workflow | forms +2/−0, +4/−1, +4/−2, +11/−0 (never lost); CDP −660, −509, −1,664, −1,345 per run (median −5 to −18 per posting); attempt time neutral (−263 … +210 s) | `applications/workflow._navigation_wait` default `none` |
| E01 | shadow-DOM deep query | **corpus-neutral**: this corpus has no SmartRecruiters/Teamtailor postings (56 unknown, 18 Workday, 7 Refline …); shallow −10/+2, −8/+2 (r1, slow stretch) and +8/−1, +3/−4 (r2) — noise. Accepted on direct evidence: SmartRecruiters `form_schema` 0 → 8 fields + 2 uploads; 14 such employers in the ATS map | default |
| E04 | per-window worker tabs (headed) | forms **+18/−2, +9/−2, +10/−6, +13/−6** against the three headed controls; the gains are the Abacus jobportal pages that paint nothing in a hidden tab; time mixed (−282 … +726 s) | `parallel(own_window=True)` / `BH_PARALLEL_OWN_WINDOW=1`, opt-in for headed runs; not default (ten windows on a desktop) |
| E03 | `hidden_blank` terminal state (headed) | fired on exactly 4 postings (jobportal, pastaHR), saved 3.6–5.5 s on three, cost 4 s on one; corpus forms neutral (+10/−0, +2/−1, +0/−3, +2/−2), time sign flips | kept: names the cause, never lost a form; `activate_tab()`/E04 is the remedy |

### Rejected

| # | theory | evidence |
| --- | --- | --- |
| E15 | locate 25 → 15 s, transition 15 → 10 s | forms **+0/−16, +0/−18** (r1), +0/−7, +1/−4 (r2); attempt time *worse* (+315, +385, +75 s): a timed-out locate burns its budget and finds nothing |
| E20 | isolated browser contexts per worker | forms +1/−9, +2/−10, +7/−11, +12/−11; +650 … +1,013 s; +200–1,170 CDP |
| E09a | 6 workers | forms +1/−4, +0/−7, +1/−3, +3/−3; +270 … +620 s; wall 230–240 s vs ~120 |
| E09b | 14 workers (`worker_limit` raised) | r1 +1/−6 & +552 s, r2 +3/−0 & −341 s — opposite signs; keep 10 |
| E18 | HTTP HEAD preflight to skip 404/410 | r1 invalid (DNS outage, 28 `ERR_NAME_NOT_RESOLVED`); r2: 0 links to skip on a fresh corpus, 84 s of HEAD requests per run (median 0.83 s each), forms noise |
| E19 | fresh tab per item | +0/−4 & +169 s, +1/−2 & +60 s (r1); +1/−4 & +180 s, +5/−3 & −57 s (r2) — no gain, usually slower |
| E22 | `BH_CDP_TRACE=0` | forms noise, time +100 … +770 s (noise): tracing is free, keep it |
| E12 | block trackers/analytics/consent scripts | r1 −495/−244 s looked real; r2 +10/−12 s, forms ±3 — not reproducible |
| E16 | `empty_stable` 5 → 2.5 s | forms ±2; time −158/+88 (r1), +33/−122 (r2) — no effect |
| E17 | `usable_stable` 0.8 → 0.4 s | forms neutral; time +34/+280 (r1), −204/−246 (r2); fewer CDP calls (−41 … −929) that never became forms or seconds |

### No robust effect (would need more replicates on a quiet machine)

| # | theory | evidence |
| --- | --- | --- |
| E10 | `wait_until=DOMContentLoaded` | r1 +2/−4 & +579 s, +2/−2 & −218 s; r2 +4/−2 & +54 s, +13/−2 & −152 s; CDP −485 … −1,649. Forms lean positive in r2 only |
| E13 | block images/fonts/media | r1 +21/+91 s (flat); r2 **−258/−280 s, navigate median −2.5/−3.0 s, +5/−2 forms** against both controls. The single most promising speed lever here, unconfirmed |

### Measured, not a switch

* **E24 decision proxy.** Of the 500 ATS-map employers, 8 final pages were shadow-DOM
  forms the old queries could not see and 16 were hidden-blank pages the old harness
  reported as empty — 24 places where the model would have had to guess or write raw JS.
* **E25 filter off.** Survived once the frame cap was raised, but delivered 1.34 GB /
  106,615 frames to the client against 17–25 MB filtered. The reader thread parses all of
  it; that is CPU the workers do not get.
* **E05 digest bytes / E06 induced disconnect.** See the section below.

## E05 / E06 — browser-only runs

**E05 — what one read hands the model** (97 apply pages of the corpus, 5 workers,
median bytes of the JSON the helper returns):

| read | bytes | note |
| --- | ---: | --- |
| `open_page(url)` defaults (6,000 chars, 20 links) | **14,032** (p90 21,444) | blocks 6,456 B carrying 3,399 chars of text — the block envelope (keys, refs, digests) costs about as much as the prose |
| `read_page(max_chars=0, max_links=0)` | 989 | url, title, `rendered`, `challenge`, counts — a metadata read is 14× cheaper and needs no new helper |
| second `read_page()` of the same page | 1,713 | the semantic cache doing its job (`unchanged_refs`) |
| `snapshot()` | 1,758 | |
| `form_schema()` | 298 | job ads, not forms — the number a form-hunting loop reads first |
| `find(pattern="apply|bewerb…")` | 116 | |

Verdict: accepted as *guidance*, not code — `max_chars=0, max_links=0` already is the
metadata read; the ATS-map chain used it. The block envelope's 2× overhead on the prose
is a real cost worth a later look (drop per-block digests from the emitted form).

**E06 — Chrome killed mid-run** (100 `open_page` items, 5 workers, browser killed at
item 40 by the driver, `BH_PARALLEL_STOP_ON_DISCONNECT` on vs off):

| | on (new) | off (old) |
| --- | ---: | ---: |
| items completed before the kill | 38 ok + 8 timeout | 41 ok + 5 timeout |
| records `browser_disconnected` | 54 | 54 |
| …of which "did not start" (resumable set) | **53** | 0 |
| …of which failed attempts after the kill | **1** | **54** |
| seconds from kill to run end | 19.9 | 20.4 |

Verdict: accepted for reliability and resumability, not for speed — either way the run
ends ~20 s after the kill (the in-flight navigations' own timeouts), but the old behaviour
records 54 attempts that never touched a page as failures indistinguishable from real
ones; the new one hands back the exact set to resume.

## What changed in the code (all default unless noted)

`harness/connect/client.py` EVENT_FILTER · `harness/connect/daemon.py` `_Peer.methods`,
`_slim_event`, frame cap, `peer_gone` · `harness/ops/parallel.py` quiet window 50 ms,
`own_window`, stop-on-disconnect (`BH_PARALLEL_STOP_ON_DISCONNECT`) ·
`applications/workflow.py` strict `usable_after` · `applications/state.py` deep query,
`hidden_blank`, stability windows from env · `tools/collect_job_form_telemetry.py`
timeout/tab/context/preflight knobs. The rejected switches stay as one-line env knobs so
the next corpus can re-ask the question; the defaults are what the data chose.

## Caveats

* One corpus, one evening, one machine that was also someone's desktop. The rejections
  with unanimous direction (E15, E20, E09a) will hold; E13 deserves a second look.
* C-01, E01-r1 and E10-r1 ran before Network slimming landed and were evicted in their
  last second; their verdicts do not hinge on those arms.
* The two "gained" lists for E04 are the same employers every time. That is the strongest
  kind of evidence this corpus produces and it points at one mechanism: pages that paint
  only while visible.
