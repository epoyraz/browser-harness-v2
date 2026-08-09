# Application-skill planner injection — 100-job A/B/A

Date: 2026-08-09. Browser: the same attached Chrome profile. Concurrency: 10 tabs. Each
run processed the same 100 `jobs.json` entries under the dry-run boundary; submissions: 0.

## Result

| Run | Skills | Batch wall | Forms reached | Fully fillable | Workflow failures |
|---|---:|---:|---:|---:|---:|
| Control A1 | off | 156.6 s | 60 | 9 | 2 |
| Candidate B | on | 183.7 s | 58 | 9 | 3 |
| Control A2 | off | 169.8 s | 56 | 7 | 3 |

The candidate matched 67 jobs across all 16 public application skills and delivered 83,384
bytes of digest-verified context to compatible planners. Resolving and loading matches for
all 100 start URLs locally took 7.2 ms median (100 repeated passes), with zero CDP calls.

## Interpretation

This experiment does **not** demonstrate a speed or reliability improvement. The corpus
planner is deterministic and makes zero model calls; it accepts the third argument for
instrumentation but deliberately does not interpret skill prose. Therefore skills cannot
change its navigation or field decisions.

Eleven jobs changed status between A1 and B, including both gains and losses. Those changes
occurred before planner invocation and were navigation, lifecycle, renderer, or CDP
timeouts. The trailing no-skill run also changed from 60 to 56 forms, demonstrating live
site/order variance larger than the 7 ms local resolver cost. Do not attribute the 17.3%
A1→B wall-clock difference or the form-count changes to skills.

## What is now proven

- URL matching and body loading add no browser round trip.
- Public bodies remain delimited untrusted reference material.
- Digests are verified before planner delivery.
- Two-argument planners remain compatible.
- Model-backed planners can receive the exact context, provenance, byte count, and hash.
- The result and journal expose which skills were delivered.

The next reliability experiment must use a planner that actually interprets the context,
against a stable replay/cassette or human-labelled decision corpus. Re-running live pages
alone cannot isolate a prose-guidance effect from site instability.
