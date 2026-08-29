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

## Addendum, 21:00–22:30 — skills and a route cache, with and without

The 08-09 A/B/A could not show a skill effect because the rules planner does not read
prose. This round made skills **machine-actionable** — a fenced ``json`` ``apply`` block
(`applications/skills.py`): ``mode``/``ats`` feed the dispatch filter, ``renders_hidden``
names the paint-only-when-visible vendors, ``routes`` are learned posting → application-view
URLs tried route-first — and built two corpora from the day's data
(`../experiments/build_skills.py`): **static** = 42 vendor skills + 438 company skills
from the ATS map; **cache** = static + learned routes from eight control runs, kept only
when the same `(posting, form URL)` pair recurred in ≥ 2 runs (33 routes on 16 hosts; the
44 single-run routes were session-token URLs). Seven arms, ABBA: `CS-01/02/03` (skills
off), `S-static-r1/r2`, `SC-cache-r1/r2`; three arms were re-run after two bugs surfaced
(below). Controls: 49 / 46 / 43 forms, 127–184 s.

| arm | matched postings | skipped by skill | forms vs CS-01 / CS-02 / CS-03 | attempt-s vs controls | route-first hits |
| --- | ---: | ---: | --- | --- | ---: |
| S-static-r1 | 60 | 2 | +0/−8, +0/−5, +1/−3 | +428, +495, −77 | 1 |
| S-static-r2 | 60 | 2 | +2/−4, +4/−3, +5/−1 | +240, +307, −265 | 2 |
| SC-cache-r1 | 82 | 2 | **+2/−13, +1/−9, +2/−7** | **+1,019, +1,087, +514** | 18 of 33 |
| SC-cache-r2 | 82 | 2 | **+1/−13, +1/−10, +2/−8** | **+902, +969, +397** | 17 of 33 |

**Static skills: neutral.** Forms move both ways within noise and time flips sign. The
one mechanical effect is real and unanimous: the two postings skipped as `account`
(Ansam, Teamtailor) were `authentication_required` in 38 of 40 control runs and never a
form. On this corpus that is all a mode hint can buy — joblens already declares the mode
for 44 postings and the dispatch filter already skips 20 of them.

**Route cache: rejected, and the reason matters.** Even reproducible cached URLs, with a
fallback to the posting when the landing fails, lost 7–13 forms and cost 400–1,090
attempt-seconds on both replicates. Of 33 routed postings per run, 17–18 landings were
accepted and filled, 6–7 were rejected by `prepare_application` and filled only after
falling back, 3 hit a landing error, and ~8 never filled (Abacus jobportal ×3, Workable
×2, Ashby, EPAM, SuccessFactors). The workflow's own comment predicted this: the old path
reaches the application view *with the posting's cookies*; a cold landing on the same URL
is a different page for most ATSs. And a route that does work still pays the route-first
probe (≤ 12 s) plus a rejected one pays a `goto` back — so the cache is slower even when
it is right. **A URL is the wrong artifact to cache.** What survives a session is the
resolved *action* — the apply control's label/selector on the posting — which is what
Stagehand caches and what `borrowed-ideas-2026-08-29.md` ranks first. That is the next
experiment; it needs the apply-control hint in `prepare_document` that this round left out.

Two defects found by the failed arms, both fixed: (1) `locate_application` raised on a
failing route-first `goto` instead of falling back — 11 of 12 cache losses in the first
SC arm; (2) `hidden_blank` fired in **headless** Chrome, where hidden tabs do paint —
it is now headed-only. A third loss was self-inflicted: rebuilding a corpus while an arm
was indexing it produced 40 `skill_integrity_failed` records (the digest check working as
designed); the arm was discarded and re-run.

## Addendum, 22:30–23:30 — the action cache

Built as proposed above: skills carry ``apply.actions = [{label, selector}]``, learned
from run records (the control whose click reached the form, per host) and from the
ATS-map chains (the chosen apply candidate per vendor); `prepare_document` takes an
``apply_hint`` and a matching control outranks the heuristic pick (a hinted label outside
`APPLY_TEXT` still qualifies; `apply_control.hinted` records whether the hint won).
Corpus: 31 vendor skills with labels + 3 run-learned hosts; **43 of 100 postings hinted**.
Arms `CA-01/02/03/04` (off), `A-actions-r1/r2` (on). CA-03 and the first A-r2 fell in a
network outage (DNS 1.8–2.3 s, 54 and 22 navigation timeouts) and were discarded; A-r2
was re-run, CA-04 added.

| arm | hinted postings | hinted clicks → form | forms vs controls | attempt-s |
| --- | ---: | ---: | --- | --- |
| A-actions-r1 | 43 | 5 → 4 | +1/−6 (CA-01), +2/−4 (CA-02), +0/−3 (CA-04) | +429, +67, +447 |
| A-actions-r2 | 43 | 9 → 8 | −16 vs CA-04 (22 timeouts: network) | +824 |

**Verdict: neutral — not a gain on this corpus, and the reason is instructive.** On every
posting where the hinted control was the one clicked (5 in r1, 8 in r2), the outcome was
**identical to the control's**: the deterministic scorer in `prepare_document` already
resolves the apply control the cache would supply. The cache only wins where the scorer
fails, and on this corpus those postings (`usable_ui`/`cycle` terminals) are ones with no
apply control on the page at all — a cached label cannot click what is not there. The
run-level form deltas are the ±3–5 corpus noise plus the outage.

Where it *would* pay, and the data says so indirectly: (1) a **model** planner, whose
every resolution costs a decision — the write-back slot now exists (`apply.actions`) and
the ATS-map chain-follower paid ~1,000 such decisions; (2) sites where the scorer picks
the wrong control (none observed here in 20 control runs — the heuristic's precision on
this corpus is the real finding); (3) the selector, once records carry it (old records
had only labels). Kept in the tree as the mechanism it is; no default depends on it.

## Addendum — record once, replay the next posting at the same employer

> **Decision (end of day): not pursued.** Worth exploring, not worth keeping: per posting
> at responsive sites a replay is 1–4 s against 7–20 s of discovery, but the corpus wall
> clock never moved in nine paired runs, and a library of 500 sites is 500 things that go
> stale. The working branch treats every link as unknown again — no corpus, no recording,
> no replay. Everything below (code, rig, recordings, results) lives on branch
> `replay-exploration-2026-08-29`; what the exploration taught the plain path stayed:
> tiled visible windows for hosts that render blank while hidden, personas and specimen
> documents, the upload resolver, the screencast fixes.

The cache experiments above steered *discovery*; this one removes it. `applications/replay.py`
records a **program** from one discovery (the apply control at each hop by selector and
label; every field the planner filled by selector, label, kind, name and semantic — **no
values**) and replays it for the next posting at that employer: navigate → click → one
batched fill → verify. Values are re-planned at replay time for whoever is applying
(`ontology.plan_for`), so a program recorded as Max Mustermann fills Martina Musterfrau's
details when she applies. Driver: `tools/replay_corpus.py` (`BH_REPLAY=auto|record|0`,
`auto` default: new sites discover and record themselves; personas via
`BH_APPLICANT_PROFILE`). Recovery: preflight (required fields *this* applicant cannot
answer → early return, no navigation), wall/expired detection, kind check on every resolved
field, self-heal by label from one `form_schema()` read, new-required-field check after the
fill, fingerprinted recordings with retire-after-2-failures, fallback to discovery + re-record.
Telemetry: `mode: replay` on every journal row plus one `application_replayed` note.

**Smoke, 6 postings (headless):** Kanton Luzern second posting **0.9 s / 7 of 7 fields**
vs 9.1 s discovery; RUAG **3.1 s / 6 of 6** vs 16.3 s.

**Headed corpus, own-window tabs** (H-replay-1 records as Max; H-base-1 discovers as
Martina; H-replay-2/3 replay Max's recordings as Martina):

| arm | replays verified | fell back (all recovered) | needs_input | persona read-back | forms lost vs baseline |
| --- | ---: | ---: | ---: | --- | ---: |
| H-replay-1 (Max) | 6 / 6, median 3.9 s | 1 | 25 | Max 6/6, Martina 0 | 0 |
| H-replay-2 (Martina) | 8 / 8, median 5.0 s | 5 | 43 (preflight too strict — fixed) | Martina 8/8, Max 0 | 0 |
| H-replay-3 (Martina, fixes) | 11 / 11, median 6.7 s | 21 (network window: 4 × no `load` in 25 s) | 22 (all tailored/unlabeled questions) | Martina 11/11, Max 0 | 7, all timeouts |

Paired on the same second-or-later postings, replay vs discovery: 3.9 vs 5.5 s (r1) and
5.0 vs 7.5 s (r2); within H-replay-3's slow window, 6.7 s replay vs 26–38 s discovery.
**A verified replay never lost a form; every failed replay fell back.** Across all headed
and headless replay arms: 62 verified replays, 0 forms lost to a verified replay.

**500-employer recording pass** (`tmp/jobs_top500.json` from the ATS map, headed, Max):
360 employers visited (140 skipped up front as account walls/email from the map), 216 forms
reached, **198 recordings for 165 employer hosts in 9.1 minutes** (175 one-click programs,
23 direct forms, median 10 fields). What discovery could not plan: 208 required fields
with no known semantic (tailored questions, unlabeled controls), then referral source (15),
country (11), tailored responses (9) — the list the preflight will hand a new applicant.

**Record-then-replay on the 100-job corpus** (headed, 10 own-window tabs, Martina, `auto`
against the 165-host library; run 1 replays what the 500-pass recorded and records the
rest, run 2 replays everything it can):

| | run 1 | run 2 |
| --- | ---: | ---: |
| wall (100 postings, 53 employers) | 237 s | 220 s |
| replays verified | 14 / 14, median 5.5 s | 13 / 13, median 4.6 s |
| replays that fell back (all recovered by discovery) | 15, median 9.2 s | 17 (15 recovered), median 7.8 s |
| plain discovery | 30 (9 forms), median 10.9 s | 23 (3 forms), median 9.8 s |
| `needs_input` early returns (0 s, no navigation) | 21 | 27 |
| forms filled | 38 | 31 |
| persona read-back (Martina in the form, Max absent) | 14 / 14 | 13 / 13 |

Paired on the 30 postings both runs filled: median 7.9 s → 6.9 s; the 13 replayed in
run 2 went 6.3 s → 4.6 s. Run 2 filled 7 fewer forms: 6 were preflight early returns on
recordings made in run 1 — three Kanton Luzern postings on a French "Lieu *" line the
ontology did not know as *city* (fixed: a bare "Lieu"/"Localité" label is the town line),
BKW's password pair (account creation), ONLU's "Biografie", Advertima's cover letter and
visa question — and 2 AutoForm navigation failures. The 8 fallbacks with "batched fill:
6/9 succeeded" are one employer whose replay fills 6 of 9 fields and is then rediscovered
for the same 6 of 9: a replay that fails only where discovery also fails should count as
verified — the next thing to measure.

**The user's own profile vs a scratch profile** (`P-base-1`: the same 100 postings,
Martina, 10 workers in their own windows in the user's running Chrome, pure discovery,
nothing recorded): **82 s wall** against 237 s / 220 s on the scratch profile, 53 forms
of 80 attempted (scratch runs: 38 and 58), median 6.8 s per posting. Paired with H-base-1
(scratch, 58 forms): 51 postings filled in both, median 6.1 s vs 4.9 s per posting; gained
BCG (the Phenom page that never fires `load` on a cold profile renders fine with the
profile's cookies: 204 s → 45 s for that employer) and MSC Cruises; lost ti&m ×5 and
finnova ×2 — both the Abacus jobportal SPA, which reached `no_application_form` at hop 2
in the parallel run and fills fine when opened alone in the same profile (probe: 23
controls, form processed). Ten overlapping windows on a desktop leave the SPA unpainted;
the profile is not the cause. The critical path moved from BCG (204 s) to ti&m (57 s).

**Tiled worker windows — removed again the same night.** The user wants tabs, only tabs,
and no code that can open a window: `own_window`, `new_window` and `place_window` are gone
from master (they live on the exploration branch). What was measured, for the record: `Target.createTarget(newWindow, background)` stacks
every worker window at one cascade position behind the user's foreground app — the user
saw none of the ten windows, and Windows stops painting occluded windows. Same run as
above with the windows tiled in a 4×3 grid (`P-base-2-tiled`): **76 s wall, 58 forms of
80** (the day's best, equal to the scratch H-base-1), ti&m ×5 and finnova ×2 back, EPAM
and one BCG posting lost to flakiness; median 5.8 s vs 6.1 s per posting. Accepted.
With **12** tiled workers (`P-base-3-tiled12`): 74 s, the same 58 forms, 6.7 s median per
posting — no gain: the wall is the largest employer group (ti&m, 7 postings in one worker,
51 s), because the driver keeps an employer's postings on one worker for replay.

### Replay, second pass (22:00–23:30, the user's profile, tabs by default)

The first corpus-level pairs on the profile were poor (RP-1/2: 109 s → 103 s, 48 → 34
forms; RP-4/5: 163 → 171 s) and the stage timer said why: a replay's **fill takes 170 ms**
while its navigation took 15–21 s. Four defects, each fixed and re-measured:

1. **Popups from hidden tabs.** Workers are tabs in one window now; a synthetic click on a
   `target=_blank` apply link in a hidden tab opens no popup, so the replay stayed on the
   job page and found none of its fields ("N of N recorded fields not found" at RUAG,
   Kanton Luzern, Brack, Vaudoise, aity, EPAM). Replay now navigates to the link's `href`
   (resolved against the page), as discovery does. RUAG hidden-tab replay: 0/7 → 7/7.
2. **Waiting for `load`.** Replay waited for the apply page's `load` (fonts, trackers)
   and then a generic state wait. It now navigates on `DOMContentLoaded` and waits for the
   *first recorded field* to exist (`_settle`, event-driven), falling back to the state
   machine only to tell a wall from a slow page. Kanton Luzern replay 20.8 s → **1.1 s**,
   RUAG 16–22 s → **3.3 s**.
3. **Partial fills re-discovered.** A replay that filled 6/9 fell back and discovery
   filled the same 6/9 (the other three: a select with no option, widgets needing real
   interaction). A partial is now verified when every unfilled field is optional, a known
   gap of the recording, or a fill-engine limit discovery shares (`_unfilled`).
4. **Hidden-blank hosts and changed apply controls.** Replay uses discovery's window rule
   (learnt hosts get a visible tiled window; a failure in a hidden tab is retried once in
   a window and the host learnt), and heals a missing apply control with discovery's own
   scorer before giving up. The verifier compares new required controls by label as well
   as selector, so widget mirrors no longer count as "new required fields".

Also: the driver runs each employer's first posting in phase 1 and every further posting
as its own item in phase 2, so seven ti&m postings replay side by side instead of queueing
on one worker; learnt account walls are skipped (`experiments/wall_hosts.json`).

**RP-6 (record) → RP-8 (replay), back-to-back, 100 postings, 10 tab workers, Martina:**

| | RP-6 record | RP-8 replay |
| --- | ---: | ---: |
| wall | 164 s | 171 s |
| forms filled | 46 | 41 |
| `needs_input` early returns (0 s, list of missing fields) | 7 | 20 |
| reachable forms (filled + needs_input) | 53 | **61** |
| replays verified | 14 | **29** (13 partial, 23 persona-verified, 6 unverifiable read-back on Gem/Ashby) |
| replay fell back | 10 | 9 (ZKB ×4 form changed, RUAG ×3 network timeouts, ti&m ×2) |
| replay time | — | 1.2–4.6 s for a third of them; median 11 s; tail 16–26 s |

Where the median goes: `start` (job page to `DOMContentLoaded`) median 2.0 s, `step`
(apply page until its first field exists) median 9.0 s, resolve 78 ms, plan 1 ms, fill
202 ms, verify 10 ms. The tail is the sites' own render time in tonight's degraded
network (discovery in the same run: 18–23 s per posting, "no DOMContentLoaded in 25 s"
timeouts at RUAG): the Abacus SPA (ti&m) takes 15–22 s to paint its form, aity's job page
16 s, Vaudoise's apply page 16 s. The harness no longer adds waiting on top of that.

**Honest verdict.** Per posting, at responsive sites, the promise holds: a second
application at the same employer takes 1–4 s against 7–20 s of discovery, filled for the
current applicant with documents attached. At corpus level tonight the wall did not move,
for three reasons that are not the replayer: only 31 of 100 postings are second-or-later
at their employer (and 26 are skipped walls); 20 postings stop at preflight because
Martina cannot answer a required question (tailored questions, passwords) — by design,
0 s each, with the list; and the network degraded ~2× during the evening, so back-to-back
runs disagree by more than replay saves. The next lever is not in replay: it is the 26 +
20 postings that never reach a fillable form.

Limits, measured: Gem/Ashby/Workable forms carry no authored ids, so selectors are
positional and labels are the real key (hence self-heal); pages that never fire `load`
(Phenom/BCG) pay the strict wait in both discovery and replay; shared hosts (the Abacus
jobportal) need the employer's own recording tried first (done). The replay share of a
fresh corpus is bounded by how many employers repeat in it — 26–32 of 100 postings here.

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
