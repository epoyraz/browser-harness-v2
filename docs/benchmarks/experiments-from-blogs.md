# Experiments from harness-engineering sources

Date: 2026-08-07

This is a research-to-experiment backlog, not an architecture roadmap. An idea earns a
place in browser-harness only if a controlled experiment shows fewer meaningful model
decisions, less model-visible context, or better correctness and safety. If it does not
move a measured outcome, delete it.

## Sources and the parts worth carrying forward

| Source | Useful idea | What not to copy |
|---|---|---|
| [The Bitter Lesson for Agent Harnesses](https://browser-use.com/posts/bitter-lesson-agent-harnesses) | The harness should reduce model decisions rather than optimise primitives that are already cheap. | More runtime abstractions whose only benefit is a few milliseconds. |
| [Rob Earlam's harness-engineering series](https://robearlam.com/blog/an-introduction-to-harness-engineering) | Small authoritative handoffs, external state, resumability, and validation before progression. | A PO/Design/Lead/Build/QA agent chain for browser execution. |
| [Learn Harness Engineering](https://walkinglabs.github.io/learn-harness-engineering/en/) | Fresh-session tests, controlled component ablation, progressive disclosure, explicit completion evidence, and deleting harness parts that no longer contribute. | WIP, loop, graph, and multi-agent machinery by default. |
| [Harness engineering for coding agent users](https://martinfowler.com/articles/harness-engineering.html) | Put deterministic computational guides and sensors before expensive inferential decisions. | Treating every possible quality dimension as an always-on sensor. |
| [Maintainability sensors for coding agents](https://martinfowler.com/articles/sensors-for-coding-agents.html) | Failure-only summaries, standard sensor schemas, historical sensor usefulness, and raw evidence kept outside model context. | A continuously running sidecar, broad coupling dashboards, or feedback overload. |
| [Context Engineering for Coding Agents](https://martinfowler.com/articles/exploring-gen-ai/context-engineering-coding-agents.html) | Load context on demand, make its cost visible, and add guidance gradually. | Copying large shared rule packs without measuring their relevance. |
| [TRAE guide, parts I](https://www.reddit.com/r/Trae_ai/comments/1sti4jx/the_definitive_guide_to_harness_engineering_what/) [and II](https://www.reddit.com/r/Trae_ai/comments/1sti5pe/the_definitive_guide_to_harness_engineering_how/) | Explicit reduction rules, injection boundaries, external state, typed contracts, and resource budgets. | Control/data planes, workflow engines, tiered memory, policy gateways, or MicroVMs for a local browser tool. |

## Experimental rules

1. Keep the model, task, corpus, and safety constraints fixed while changing one thing.
2. Measure meaningful model decisions, input and output tokens, uncached input, helper and
   CDP calls, wall time, retries, outcome accuracy, blocker recall, and submissions.
3. Preserve the raw journal and artifacts outside model context so compact output remains
   retrievable without pretending that a lossy summary is reversible.
4. A candidate must preserve all safety invariants and be non-inferior on the 23-form
   corpus. A token or latency win cannot compensate for a missed blocker.
5. Revert a candidate that has no material impact. Do not keep speculative scaffolding for
   a future experiment.

## Current control

The existing application benchmark provides the comparison point:

| Metric | Control |
|---|---:|
| Substantial forms | 23 |
| Successful dry runs | 23/23 |
| Technical blockers retained | 4/4 |
| Applications submitted | 0 |
| Original live-run model invocations | 46 |
| Polling-only invocations | 37 |
| Latest clean-path model invocations | 1 |
| Latest clean-path intermediate calls | 0 |
| Latest clean-path input tokens | 134,945 |
| Latest clean-path cached input tokens | 133,888 |

The absolute input total is mostly an inherited cached conversation prefix. Repo-owned
experiments can prevent further live-context growth, but they cannot shrink a prefix that
the caller has already supplied.

## Five-experiment run: 2026-08-08

Five candidates ran in parallel against the same recorded application result. Experimental
code is not retained merely because its own tests pass.

| Candidate | Result | Evidence | Decision |
|---|---|---|---|
| Failure-only decision pack | The selected raw attempts were 124,153 bytes / 35,932 `o200k` tokens; the final compact pack was 906 bytes / 232 tokens with all outcomes and 4/4 blockers unchanged. Removing pretty-print whitespace alone reduced the earlier model-targeted pack from 363 to 223 tokens; the deterministic next action brought the final pack to 232. | Exact pack equality plus recorded 23-form assertions. | **Keep** the compact `--pack` output. |
| Fresh-context resume | Two artifacts worked at 1,222 bytes, but a single 906-byte pack with `next_action` worked too. A no-history judge returned every count, the correct blocker action, and `safe_to_submit: false` in one model invocation with no tool call. | Independent `fork_turns=none` judgment. | **Keep** one field in the existing pack; **delete** the separate state generator. |
| Generic context policies | The actual already-compact decision pack projected from 863 to 863 bytes. The claimed large reduction came from an artificially padded input, and no runtime or benchmark consumer used the extra decision policies. | Independent review plus recorded-pack comparison. | **Delete** the prototype; the decision pack is the exercised allowlist. |
| Computational gate | One blocking tool decision ran the real-Chrome 23-page corpus, validated the pack, and returned one final result: 23/23 schemas, 0 external requests, 92 CDP calls, 4/4 blockers, and 0 submissions. It made 0 intermediate tool calls. The always-loaded instruction was then compressed from 147 to 71 `o200k` tokens without changing the rule. | Real Chrome, not a mocked browser. | **Keep** the orchestration rule; add no runtime abstraction. |
| Pack-level mutation challenge | Its checker caught 6/6 seeded pack edits, but the hard-coded approved pack was both the control and oracle. It did not mutate browser behavior or the pack generator and could accept unrelated private fields. | Independent adversarial review. | **Delete** the self-referential benchmark; add a real browser/input mutation only when one protects an observed failure. |

The rejected context, mutation, and separate-state prototypes added far more code than the
accepted change. Their results stay documented, but their code does not stay in the
repository.

## Pi principles: apply the boundary, not the architecture

The Pi principles supplied after this run reinforce the same delete-first direction, but
browser-harness is a browser instrument rather than a complete agent shell. Copying Pi's
literal four-tool surface would remove the compound browser operations that already cut
model decisions.

| Pi principle | What already fits | Decision for browser-harness |
|---|---|---|
| Minimalist foundation | One programmable `bh` entry point plus `js()` and `cdp()` escape hatches. High-level helpers share that one invocation rather than becoming separate model tools. | Keep a small documented surface, but retain a compound helper only when it measurably removes decisions. Continue instruction ablation; do not reduce the API to four generic operations by taste. |
| Adaptability over prescription | Arbitrary Python and user/project `bh_helpers.py` files let the caller build its own workflow. | Keep this seam. Do not add prescribed stages, graph orchestration, or another extension framework. |
| Transparent control | Typed outcomes, local JSONL journals, trace rendering, screenshots, and recordings expose browser actions and evidence. | Keep observable action/outcome events local and return failure-only summaries to the model. Do not capture hidden reasoning or inject raw event streams by default. |
| File-driven context | `SKILL.md`, local Markdown skills, and project helper files already carry behaviour outside runtime code. | Let the calling agent resolve `AGENTS.md`; do not build a second parser. Test a short root router and direct one-skill injection before expanding the current registry. |
| Local and model agnostic | The runtime controls local Chrome over CDP and calls no model SDK. | Preserve this by rejecting an embedded model router or provider adapter. Keep provider-specific transcript analysis in benchmark tooling only. |
| Inspectable history | Append-only journals, recordings, traces, and cassette replay are readable local artifacts. | Keep the current level. A generic branch/fork/rollback engine would be dishonest for irreversible browser side effects; use dry-run safety, isolated contexts, and replay instead. |

Only two Pi-derived probes deserve priority:

1. **Short-context ablation:** compare a root router under 1,000 tokens with the current
   skill on fresh navigation, scraping, form, recording, and parallel tasks. Keep only a
   non-inferior router that reduces always-loaded tokens without adding a discovery call.
2. **Direct domain-skill A/B:** inject one local ATS Markdown skill directly versus no
   skill. The current `harness/skills.py` registry is 259 lines and is reached only through
   manual CLI commands; freeze it during the test and delete it if direct file context does
   not reduce decisions or tokens.

No AGENTS loader, thought-stream recorder, live event bus, checkpoint database, model
adapter, or rollback engine is justified by these principles alone.

## Ranked experiments

### 1. Failure-only decision pack

**Sources:** Fowler's computational sensors and sensor-summary follow-up.

**Hypothesis:** Returning only safety state, aggregate outcomes, retries, and actionable
technical blockers will preserve every downstream decision while avoiding raw application
logs and successful field details in model context.

**Change to test:** Use the compact output from
`tests/bench/application_decisions.py --pack`. Raw attempt logs, journals, screenshots, and
videos stay on disk.

**Keep only if:** all 23 outcomes, all four blockers, the retry count, and `submitted: 0`
match the full report; one model invocation is sufficient; model-visible application output
is at most 500 tokens.

**Reject if:** any classification, blocker, or safety evidence is lost.

**Result:** Passed. The final pack is 906 bytes / 232 `o200k` tokens, a 99.4% reduction
from the selected raw attempts.

### 2. Fresh-context resume from one decision pack

**Sources:** Earlam's external artifacts, WalkingLabs' fresh-session test, and TRAE's state
separation principle.

**Hypothesis:** A fresh model session can make the same next decision using only the compact
decision pack, without receiving the accumulated transcript.

**Change to test:** Add a deterministic `next_action` to the existing pack and inject that
pack directly into a fresh session. Do not add a state framework or product dependency.

**Keep only if:** the fresh session produces the same safety judgment, outcome counts,
blocker diagnosis, and next action in one meaningful decision, with less than 1,000 tokens
of application-specific input.

**Reject if:** restoring the decision requires reading raw journals by default or creates
another model inspection step.

**Result:** Passed. The direct-injection judge used one model invocation, made no tool call,
and returned the exact counts, all four blockers, `resolve_technical_blockers`, and
`safe_to_submit: false`. Total model input was 19,030 tokens, of which only 232 were the
application pack; the rest was caller-owned system and tool context. The separate
253-line artifact generator was deleted.

### 3. Context allowlist per decision type

**Sources:** Fowler's context interfaces and TRAE's reduction rules and injection
boundaries.

**Hypothesis:** Each decision needs a small, different authority bundle; a single generic
context dump is unnecessary.

**Change to test:** Define a data-only policy for the remaining decision classes:

| Decision | Permitted context |
|---|---|
| Accept batch outcome | safety state, aggregate outcomes, retries, blocker evidence |
| Diagnose retry | typed failure, retryability, attempted value, minimal candidates |
| Request human input | job id, field label, requirement, reason, permitted choices |
| Retrieve evidence | artifact path and an explicit bounded query |

**Keep only if:** matched decisions remain identical and total model-visible bytes fall for
every exercised class.

**Reject if:** the policy becomes a generic retrieval framework or adds a decision about
which policy to choose.

### 4. Computational gate before model wake-up

**Sources:** Fowler's computational-versus-inferential distinction and Earlam's validated
handoffs.

**Hypothesis:** Schema checks, safety checks, corpus invariants, and retry classification can
be decided deterministically before the model is involved.

**Change to test:** Chain real-browser execution, invariant checks, and compact output in
one blocking command. Wake the model only for a typed semantic failure or the final result.

**Keep only if:** clean batches create no intermediate model decision, failure batches stop
before the full run, and failure evidence remains complete.

**Reject if:** it hides a failure, automatically retries a semantic error, or adds a runtime
orchestration abstraction.

### 5. Instruction ablation, one group at a time

**Sources:** WalkingLabs' controlled exclusion tests, progressive disclosure, and periodic
harness simplification.

**Hypothesis:** Parts of `SKILL.md` are no longer necessary for current models or for common
task classes and can be removed or lazy-loaded without changing behaviour.

**Change to test:** On a fixed set of application, navigation, scraping, recording, and
parallel tasks, remove one instruction group at a time. Never rewrite the whole skill in a
single experiment.

**Keep only if:** task success, safety, and tool-choice accuracy remain non-inferior while
always-loaded tokens decrease.

**Reject if:** the evaluation has too few repeated trials to distinguish a real effect from
model variance.

### 6. Lazy domain skill for application forms

**Sources:** Fowler's on-demand skills, WalkingLabs' short-entrypoint guidance, and the
domain-skill pattern already explored in v1.

**Hypothesis:** Generic browser tasks should not pay the context cost of application-form
guidance, while form tasks benefit from loading a narrow domain skill once.

**Change to test:** Keep the base skill as a router and move only form-specific planning,
safety, and field-handling guidance into a lazy-loaded skill. Do not move browser
primitives or duplicate rules.

**Keep only if:** the 23-form outcome is unchanged, generic browser tasks load fewer tokens,
and form tasks need no extra model decision to discover the skill.

**Reject if:** routing is unreliable, instructions are duplicated, or total form-task
tokens increase.

### 7. Sensor usefulness and deletion history

**Sources:** Fowler's sensor-effectiveness history and WalkingLabs' harness-debt cleanup.

**Hypothesis:** We can identify checks that cost time or output but never change a decision,
then remove them safely.

**Change to test:** For benchmark checks only, record sensor id, execution cost, whether it
fired, whether it caught a seeded regression, and whether it changed the final result. Do
not add another live telemetry stream.

**Keep only if:** the history leads to at least one proven deletion, consolidation, or
high-value missing check.

**Reject if:** it merely creates a dashboard or collects data without a deletion decision.

### 8. Approved-fixture mutation challenge

**Sources:** Fowler's behaviour-harness discussion, approved fixtures, and mutation-testing
experiment.

**Hypothesis:** The frozen 23-page form corpus may pass while still missing important
regressions; realistic seeded faults can measure the corpus's actual detection strength.

**Change to test:** Seed a small set of high-risk mutations: skipped required fields, lost
OOPIF discovery, false autonomous-ready classification, swallowed technical blockers,
unsafe submit enablement, and incorrect retryability.

**Keep only if:** each new assertion kills a named realistic mutant and has negligible cost
on the normal corpus run.

**Reject if:** it introduces broad mutation infrastructure or tests implementation details
without protecting an outcome.

### 9. Compact, actionable failure messages

**Sources:** Fowler's guidance-enriched sensor messages.

**Hypothesis:** A typed failure that includes the allowed correction removes an exploratory
model inspection without dictating semantic choices.

**Change to test:** Compare the current failure evidence with a minimal structure containing
class, source, attempted value, candidates, retryability, and permitted next operations.

**Keep only if:** the same repair is reached with fewer model calls or fewer input tokens
across recorded failures.

**Reject if:** messages become long tutorials, duplicate `SKILL.md`, or encourage automatic
semantic retries.

### 10. Evidence retrieval instead of evidence injection

**Sources:** Earlam's externalized memory and Fowler's context interfaces.

**Hypothesis:** Most decisions need only an artifact reference; detailed evidence should be
queried only after an ambiguity is identified.

**Change to test:** Put stable artifact paths and bounded query examples in the decision
pack. Compare default injection of screenshots, journals, and field logs with retrieval on
demand.

**Keep only if:** default model-visible context decreases, ambiguity resolution remains
possible, and ordinary successful cases do not add a retrieval decision.

**Reject if:** retrieval is needed for most cases or requires a new service, database, or
vector index.

## What I would do next

1. Run experiment 5: remove one `SKILL.md` instruction group at a time against fixed model
   tasks. Deletion still has priority over new rules.
2. Try experiment 6 only if ablation proves form guidance is useful but costly when always
   loaded.
3. Revisit context policies only when a real decision needs information not present in the
   232-token pack.
4. Revisit mutation testing only with a real browser/input fault, not a self-referential
   pack oracle.

## Explicitly deferred

Do not build these unless a future measured failure cannot be solved more simply:

- multi-agent stage pipelines;
- graph orchestration or a workflow engine;
- control-plane/data-plane separation;
- an always-running telemetry or sensor sidecar;
- tiered or vector memory;
- a policy gateway for a local browser;
- automatic semantic retry loops;
- extra Chrome instances for parallelism;
- dashboards before the underlying measurements cause a concrete decision.
