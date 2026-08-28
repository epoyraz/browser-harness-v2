# How much of a 100-posting run is noise

2026-08-29. Four runs of `tools/collect_job_form_telemetry.py` over the same 100 fresh
Joblens postings (`jobs_run100.json`), 10 workers, headless, same machine, within one hour.
Dry run throughout: zero submissions, uploads off.

This exists because two experiments in one session were nearly reported as wins on
differences smaller than what the corpus produces on its own.

## The runs

| run | change | wall | forms | workflow_failed |
| --- | --- | ---: | ---: | ---: |
| baseline | — | 147.6 s | 46 | 15 |
| t1 | classification edit, **stale daemon** | 143.9 s | 46 | 10 |
| t1b | same edit, fresh daemon | 172.7 s | 44 | 18 |
| t5 | dispatch filter off | 167.5 s | 46 | 16 |

Nothing between baseline and t1b can change whether a form fills — the edit renames an
error class. `workflow_failed` still moved **15 → 10 → 18**.

## The number that matters

Comparing t1b and t5 posting by posting, over the 80 postings both runs actually
attempted:

**66 of 80 produced the same outcome. 14 flipped.**

| flip | n |
| --- | ---: |
| `workflow_failed` → `form_processed` | 3 |
| `form_processed` → `no_application_form` | 3 |
| `workflow_failed` → `authentication_required` | 2 |
| `no_application_form` → `form_processed` | 2 |
| `no_application_form` → `workflow_failed` | 2 |
| `workflow_failed` → `no_application_form` | 1 |
| `generic_form` → `workflow_failed` | 1 |

On the headline metric alone — did a form get filled — **5 postings gained one and 3 lost
one. Net +2, but 8 moved.**

So a single run of this corpus carries roughly **±3 forms and ±4 workflow failures** of
pure noise. An A/B that reports a handful of forms either way has measured the weather.
The live web is the variable: pages that time out once and load the next time, walls that
appear intermittently, hosts that rate-limit the second visit.

## What survives this, and what does not

**Matched, unanimous, per-posting comparisons survive.** The dispatch-filter check
(`BH_APPLICATION_DISPATCH_FILTER=0`) asked whether the 20 postings skipped as
`declared_mode_account` would have filled anything. Answer: **0 of 20** — 19
`no_application_form`, 1 `workflow_failed`. That is trustworthy because it is a paired
comparison of the same postings and the result is unanimous, not because the run totals
agreed. They did not: the no-filter run used *fewer* attempt-seconds in total (1,418 vs
1,510) while doing twenty more attempts, which is noise swamping a real 321-second saving.

**Run totals do not survive.** Wall clock, total attempt-seconds, and aggregate form
counts all moved more between identical configurations than between different ones.

## How to run an experiment on this corpus

1. Compare **the same postings**, joined on `job_id`, never run totals.
2. Prefer a question with a unanimous answer — "did any of these N fill?" beats "did the
   average improve?".
3. For anything else, replicate. One run is an anecdote; the earlier navigation work
   already produced −42% and −18% from two identical configurations.
4. State `n` for the matched subset, not the corpus size.

## And restart the daemon

The `t1` row is a second lesson. The daemon holds the browser websocket and raises the
errors the collector records, so it was still running code from 79 minutes before the
edit; the run reported the old classification and looked like the change had failed. The
protocol version catches protocol drift and nothing catches a behavioural fix.

**Kill the daemon before any run that is meant to measure a code change.**
