# Application decision baseline

This benchmark joins the recorded application outcomes, browser-harness journals, and
Codex token-count events. It reads tool names, timestamps, and usage totals only; it does
not read prompts, messages, or reasoning content.

## Result

| Metric | Result |
|---|---:|
| Applications in the browser batch | 58 |
| Substantial forms selected for evaluation | 23 |
| Successful dry runs | 23/23 |
| Autonomous-ready applications | 2/23 |
| Applications requiring human answers or consent | 21/23 |
| Required human fields | 72 |
| Applications with technical field blockers | 2/23 |
| Application retries | 1 |
| Applications submitted | 0 |
| Browser-harness invocations | 3 |
| Model invocations during the run | 46 |
| Polling-only model invocations | 37 (80.4%) |
| Input tokens | 8,596,367 |
| Cached input tokens | 8,557,824 |
| Uncached input tokens | 38,543 |
| Output tokens | 5,295 |
| Input tokens spent polling | 6,943,352 (80.8%) |
| Output tokens spent polling | 1,967 (37.1%) |

The browser work was already well batched: three `bh` invocations covered the original
run, its failed first attempt, and one targeted retry. The dominant waste was outside the
harness. Waiting for the long-running browser command caused 37 model decisions that only
polled process state. Replacing that polling loop with one blocking wait is therefore the
first hypothesis to test; browser micro-optimisations cannot approach its potential impact.

“Success” has two levels on purpose. A successful dry run means the substantial form was
filled as far as known data allowed, no top-level error remained, and nothing was
submitted. “Autonomous-ready” additionally means no required field needs a human answer,
consent, or technical fix.

Per-application model tokens are reported only as batch amortisations. The applications
shared model calls, so assigning exact token counts to individual forms would invent
precision that the transcript does not contain. Application outcomes, retries, and human
requirements remain exact per application in the text or JSON report.

Reproduce with:

```bash
uv run python tests/bench/application_decisions.py \
  outputs/application-form-dry-run-final/logs/attempts.jsonl \
  --manifest tests/corpus/forms/manifest.json \
  --codex-transcript /path/to/codex-rollout.jsonl
```

Add `--json` for the per-application machine-readable report.

## Experiment: block inside one tool decision

The follow-up changed orchestration only: a single tool call started two serial real-Chrome
passes over the 23-page offline corpus and handled process waiting internally. No harness
runtime code or polling helper was added.

| Metric | Result |
|---|---:|
| Real-Chrome corpus passes | 2 |
| Fixture passes | 46 |
| Model invocations | 1 |
| Intermediate model tool calls | 0 |
| Tool-call duration | 98.6 seconds |
| Schema mismatches | 0 |
| External network requests | 0 |
| Candidate helper calls | 23 per run |
| Candidate CDP calls | 92 per run |
| Input tokens | 114,972 |
| Cached input tokens | 114,432 |
| Uncached input tokens | 540 |
| Output tokens | 823 |

The workload is intentionally local and therefore does not support an absolute speed or
token comparison with the earlier live run. It does isolate the mechanism: a command can
run longer than the normal yield interval without any model decision devoted to polling.
Applying that policy to the recorded live trace would remove its 37 polling invocations;
those invocations accounted for 6,943,352 input and 1,967 output tokens.

Decision: keep the short instruction in `SKILL.md`; add no runtime abstraction. The model
already has the necessary general-purpose process primitives, so another helper would add
code without removing another decision.

## Experiment: one final result with a failure gate

The nine non-polling calls in the original trace were classified before changing anything:

| Call class | Count | Decision |
|---|---:|---|
| Pilot and full runs | 2 | collapse behind one pilot gate on the clean path |
| Partial-result inspections | 2 | replace with the final structured result |
| Source read and code patches | 3 | keep; these diagnosed and fixed a real bug |
| Plan-only update | 1 | remove from the task path |
| Corrective retry | 1 | keep when a correction actually requires it |

This means “remove all nine” was the wrong hypothesis. Five calls carried real diagnostic
or corrective work. The candidate instead makes the run return aggregate outcomes and only
the actionable failed-field evidence; 72 human-answer fields remain counts rather than a
large dump.

### Clean path

One tool decision ran the real-Chrome 23-page corpus, asserted its invariants, and emitted
the 23 recorded application outcomes plus all four technical blockers.

| Metric | Result |
|---|---:|
| Model invocations | 1 |
| Intermediate model tool calls | 0 |
| Schema mismatches | 0 |
| External network requests | 0 |
| Successful dry runs | 23/23 |
| Technical blockers returned | 4/4 |
| Input tokens | 134,945 |
| Cached input tokens | 133,888 |
| Uncached input tokens | 1,057 |
| Output tokens | 586 |

The two original inspection calls and the plan-only update consumed 547,559 input and 620
output tokens. Chaining validation and the final report to the run removes those three
decisions entirely. Absolute token totals are context-dependent, so the controlled result
is the decision reduction and complete evidence, not a claimed universal token ratio.

### Failure path

The gate replayed the real first Comparis attempt, where the timezone select returned
`no_option_match`. It stopped before the full batch and returned the field name, source,
reason, and three candidates in the same result.

| Metric | Result |
|---|---:|
| Model invocations | 1 |
| Intermediate model tool calls | 0 |
| Full batch started | no |
| Technical blockers returned | 1/1 |
| Input tokens | 137,612 |
| Cached input tokens | 135,936 |
| Uncached input tokens | 1,676 |
| Output tokens | 1,620 |

Decision: keep the minimal failed-field evidence and the skill instruction. Do not add a
decision tree or automatic semantic retry; a real failure returns control to the model.

## Experiment: compact fresh-context decision pack

The full recorded result was reduced through an explicit allowlist. Raw attempts and
journals remain on disk; the model receives only aggregate outcomes, safety state, retry
count, technical blocker evidence, and a deterministic next action.

| Representation | UTF-8 bytes | `o200k` tokens |
|---|---:|---:|
| Selected raw attempt rows | 124,153 | 35,932 |
| Full machine-readable report | 5,418 | 1,397 |
| Compact pack before next action | 863 | 223 |
| Final compact pack | 906 | 232 |

The final pack retained all 23 outcomes, all four technical blockers, the single retry,
and zero submissions. It is 99.4% smaller than the selected raw attempt rows by tokens.

A fresh no-history judge received the final JSON directly. It returned the exact outcome
counts, identified the two phone failures, chose `resolve_technical_blockers`, and refused
submission.

| Fresh-context metric | Result |
|---|---:|
| Model invocations | 1 |
| Tool calls | 0 |
| Total input tokens | 19,030 |
| Cached input tokens | 18,176 |
| Application-pack tokens | 232 |
| Output tokens | 303 |

The total still includes caller-owned system and tool context; browser-harness controls the
232-token application payload, not that fixed prefix. A two-artifact state prototype also
worked, but added 253 lines of benchmark code. Adding only `next_action` to the existing
pack produced the same decision, so the separate state generator was deleted.

Decision: keep the compact pack and one deterministic action field. Add no state framework,
retrieval service, or runtime orchestration layer.

## Always-loaded instruction reduction

The accepted single-call gate originally needed ten lines of `SKILL.md`. After the
behavioral result was fixed, the same rule was compressed rather than left as permanent
experiment narration.

| Representation | UTF-8 bytes | `o200k` tokens |
|---|---:|---:|
| Original gate instruction | 697 | 147 |
| Final gate instruction | 340 | 71 |
| Reduction | 51.2% | 51.7% |

The full skill moved from 4,360 to 4,284 tokens. The remaining 71 tokens preserve the
measured result, the no-polling rule, failure-only evidence, and the ban on automatic
semantic retries.

## Final integration gate

The retained changes passed 309 tests and Ruff. A fresh real-Chrome run then matched all
23 recorded form schemas with no external network request, using 23 candidate helper calls
and 92 CDP calls. The final 907-byte newline-terminated pack retained 4/4 technical
blockers, chose `resolve_technical_blockers`, and recorded zero submissions.
