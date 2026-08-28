# Measured: the three `perf-review.md` savings on `perf/integrated`

2026-08-28, 17:54-19:05 local. Four runs of `tools/collect_job_form_telemetry.py` on
`perf/integrated` (4ef3f28, checked out detached), same corpus (`jobs_newest.json`, 100
postings), same Chrome, same daemon, one at a time, `required.txt` present so field counts
are comparable. `control` is the same binary with every new behaviour toggled off, run in
the same hour, so live-site drift is measured rather than assumed.

Run directories, all under `outputs/`:
`job-form-telemetry-2026-08-28-control` - `-cold` - `-warm` - `-wide`.
Memo file: `outputs/employer-memo-measure.json`, deleted before `cold`, left in place for
`warm` and `wide`.

## The four runs

| run | workers | attempts | skipped (meta / memo) | forms filled | field writes | attempt-s | wall-s | forms / wall-s | mean hops (filled) | route_rule acc / fell back |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| control | 10 | 100 | 0 / 0 | **44** | 357 / 392 | 638.4 | 70.8 | 0.621 | 1.82 | - |
| cold | 10 | 72 | 23 / 5 | **41** | 324 / 356 | 372.9 | 45.6 | 0.899 | 1.80 | 3 / 0 |
| warm | 10 | 45 | 23 / 32 | **36** | 268 / 298 | 181.7 | 23.5 | 1.529 | 1.78 | 3 / 0 |
| wide | 20 | 40 | 23 / 37 | **37** | 283 / 314 | 269.8 | 27.7 | 1.335 | 1.78 | 3 / 0 |

Outcome histograms:

| run | form_processed | no_application_form | authentication_required | generic_form | workflow_failed | skipped_by_metadata | skipped_by_memo |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| control | 44 | 43 | 7 | 6 | 0 | 0 | 0 |
| cold | 41 | 22 | 5 | 3 | 1 | 23 | 5 |
| warm | 36 | 6 | 0 | 3 | 0 | 23 | 32 |
| wide | 37 | 0 | 0 | 3 | 0 | 23 | 37 |

Deltas against control:

| run | attempt-s | wall | forms filled | forms / wall-s | mean attempt | mean navigate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| cold | -265.5 (-41.6%) | -25.2 s (-35.6%) | **-3** | +44.8% | 6.38 -> 5.18 s | 6.14 -> 4.62 s |
| warm | -456.7 (-71.5%) | -47.3 s (-66.8%) | **-8** | +146% | 6.38 -> 4.04 s | 6.14 -> 3.60 s |
| wide | -368.6 (-57.7%) | -43.1 s (-60.9%) | **-7** | +115% | 6.38 -> 6.74 s | 6.14 -> 5.84 s |

## Which skip bought which second, and which cost a form

Every skipped posting joined back to what `control` did with that same `job_id` in the
same hour:

| skip | run | n | control-seconds avoided | control-filled forms skipped |
| --- | --- | ---: | ---: | ---: |
| metadata filter (`declared_mode_account`) | all three | 23 | 142.8 | **0** |
| memo `account_wall` | warm / wide | 11 | 84.0 | **0** |
| memo `no_application_form` | cold | 2 | 25.4 | 0 |
| memo `no_application_form` | warm | 21 | 155.4 | **5** |
| memo `no_application_form` | wide | 26 | 182.8 | **7** |

The three forms `cold` did not fill were attempted in `cold` and skipped by nothing: two
came back `no_application_form` and one `workflow_failed` on postings `control` filled
40 minutes earlier. That is live variance, and it is the same variance that then poisons
the memo - see below. `warm` lost the same three plus five to memo skips; `wide` lost
seven, all of them memo skips, and zero to drift.

Nothing was ever filled in a toggled-on run that `control` had missed, so none of the
deltas above are gains hidden inside a loss.

## Conclusions

The dispatch filter (2.1) is exactly what the review predicted and nothing more: 23 of
100 postings refused a tab on `apply.mode == account` alone, 142.8 of control's 638.4
attempt-seconds (22.4%) never spent, and **not one filled form lost in any of three runs**
- control's own rows for those 23 were 22 `no_application_form` and one `generic_form`.
Route-first (2.3) is correct and almost inert on this corpus: three Ashby postings match
the route table, 3/3 landed on a form and 0 fell back, which is why mean hops for a filled
form move 1.82 -> 1.78 and no further - 97 of 100 postings are not a route the table knows,
so this measures the mechanism working, not a saving. The employer memo (2.2) is where
the honest headline lives. Its `account_wall` half is free money: 11 skips, 84 control-
seconds, zero forms. Its `no_application_form` half is not. `jobs.uzh.ch` filled 2/2 in
control; one of the two drifted to `no_application_form` in `cold`, which wrote a
`count: 1` site entry, and `warm` then skipped both. The same single-observation entry cost
`jobs.ksa.ch` (1/1 filled in control), `jobs.visana.ch` (filled in control *and* cold) and
`hslu-jobs.ch`. Five forms in `warm`, seven in `wide` - a per-moment miss on a JS-rendered
portal promoted to a fact about the whole employer site, and `SITE_OUTCOMES`'s justification
for including `no_application_form` was iCIMS 4/4, an ATS, not a university job portal.
So: forms filled per wall-second went 0.621 -> 0.899 (cold) -> 1.529 (warm), but warm is
faster partly *because it filled eight fewer forms*, and that is a loss, not a win. The run
that is honestly better is `cold`: -41.6% attempt-seconds, -35.6% wall, and its -3 forms are
all attributable to live drift rather than to a skip. `wide` says the knee is below 20 on
this corpus: with only 40 attempts left after 60 skips, 20 workers is two batches of
contention - mean attempt rose 4.04 -> 6.74 s, mean fill 546 -> 914 ms, and its wall is
*worse* than `warm`'s (27.7 vs 23.5 s) despite five more skips. The change I would make
before landing the memo is to drop `no_application_form` from `SITE_OUTCOMES`, or require an
entry to have `count >= 2` from separate postings before it may skip; `account_wall` and
`authentication_required` earn their keep as they are.

Note on the tooling: the first `control` run completed all 100 attempts and then died on
`NameError: name 'SEMANTIC_CACHE_HITS' is not defined`, the last statement before
`results.json` is written - latent since 2a6ede5, where the lookup that filled the cache was
removed and its reporting stayed. The counter is now defined; the run was repeated from
scratch and the aborted artifacts kept in
`outputs/job-form-telemetry-2026-08-28-control-aborted/`.
