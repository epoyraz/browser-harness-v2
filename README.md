# browser-harness v2

Agent-facing browser control over the Chrome DevTools Protocol.

## The measurement that set the agenda

A real 70-page scrape, 1385 results, three ways:

| Mode | Time |
|---|---:|
| Agent loop, one tool call per page | ~17 min |
| One process, navigate + extract | 95.2 s |
| One process, in-page fetch, bounded concurrency | **15.6 s** |

**90% of an agent's wall clock is model decisions. Harness primitives are 0.03%.**

So the harness's job is not to be fast. It is to make the model decide less often, and to
make each decision it does need count. Every design note in `docs/DESIGN.md` is downstream
of that sentence.

## The case

Three mechanisms do the work, and none of them are domain-specific:

**Bounded output that cannot flood a context.** Every helper result and every invocation's
stdout share one `BH_OUTPUT_BYTES` ceiling. Overflow is not truncated and lost — it spills
to a content-addressed store and comes back exactly via `fetch_content(digest)`. One
accidental page dump costs a digest, not a context window.

**Reads that do not repeat themselves.** Page reads are versioned semantic blocks: an
unchanged second read returns stable references, a mutation emits only the blocks that
changed, and a document-bound cursor fails closed when stale rather than paging into a
document that moved. Measured at **−61% emitted bytes across four reads** of one posting
(`tools/semantic_cost.py`).

**Actions that answer their own question.** A click, type, select or form write returns the
changed region and the observed validation state. That replaces the
`act → read_page() → decide` loop with one call — which is one model decision instead of
two, on every interaction.

On top of those: `fill_form()` writes a whole form from its schema in one round trip rather
than one call per field, and `parallel()` fans work across tabs under a hard budget with
cooperative cancellation and lease-based cleanup.

And a way to *ask*, not only to act. Benchmark telemetry showed the agent writing 133 raw
`js()` calls across three tasks — 47 of them hand-rolled
`querySelectorAll(...).map(...)` — because the verbs were covered and the questions were
not. `find(text=…)` filters in the page, `extract(selector, fields)` returns repeated
records as rows each carrying a ref, and `form_values()` reads a form back the way
`fill_form()` writes it.

## Against v1

Measured on the two checkouts, not asserted:

| | v1 | v2 |
|---|---:|---:|
| Runtime lines | 5,900 (2,204 of it browser control) | 11,182 core |
| Dependencies | 4 | **1** |
| Test functions | 150 | **610** |
| Test lines | 2,898 | **14,164** |
| Agent-facing parallelism | none | `parallel()` |
| Run telemetry | product analytics only | append-only journal |
| Result contract | `RuntimeError(str)` | 29 typed classes + recovery |

**4.1× the tests**, and v1 has no agent-facing concurrency at all — its `http_get`
docstring says *"Wrap in ThreadPoolExecutor for bulk"*, which hands the problem back to the
caller. Its `telemetry.py` is product analytics; there is no run journal to ask what a run
did. The runtime-lines row needs the caveat beside it, which is why it has one: see *What
it costs* below before quoting a ratio.

Beyond the table: the daemon hands each client a tab nobody else has adopted, so two
clients cannot collide on one page (v1 computes that client-side); harness JS lives in an
isolated world where the page can neither see nor break it; and `bh --doctor` classifies
why a browser can or cannot be reached across macOS, Linux and Windows.

### Failures a caller can branch on

This is the difference a script feels on every line. v1's whole agent-facing error surface
is one class:

```python
if "error" in r: raise RuntimeError(r["error"])
raise RuntimeError(f"fill_input: element not found: {selector!r}")
```

A missing element, a thrown JS exception, a timeout and a dead daemon all arrive as
`RuntimeError`, so branching means matching substrings of a message. The same failure in
v2:

```json
{ "ok": false, "class": "value_rejected", "detail": "the field refused the write",
  "observed": {"ref": "e7", "wrote": "x"}, "retryable": false,
  "recovery": "the control refused or rewrote the value — try another write mode
               (insert, type) or a different value" }
```

Raised as `ValueRejected`. **33 classes, 29 typed exceptions, 20 recovery lines**, and
`retryable` computed from the class rather than guessed at the call site. `observed` carries
the evidence for the decision, never page content.

Rule 4 rides along: any operation over N items reports attempted, succeeded and failed, and
a slot the pool never filled is a *counted failure*. That rule exists because an unbounded
fan-out once returned 163 of roughly 300 results and raised nothing at all.

## What it costs, and where v1 is ahead

A README that only lists wins is not a case, it is a brochure.

**Like for like, it is closer to five times the code.** The headline 5,900 flatters v2:
**41% of v1's runtime is not browser control** — 543 lines of Browser Use Cloud auth, 1,596
of video and recording, 308 of product telemetry. Strip those and v1 drives a browser in
**2,204** lines against v2's 10,504. Even crediting the whole of v1's mixed `admin.py` to
browser control it is about 3×.

Some of that gap is owning the stack, where v2 depends on `websockets` alone. The rest is
capability v1 does not have rather than a more elaborate version of what it does: the
multi-client daemon with adopt and leases, the isolated world and ref registry,
action-consequence fusion, the semantic cache, `parallel()` with cleanup, the content store,
typed outcomes, cross-platform discovery. That is the price of the substrate thesis, and
capability you do not use is cost you still pay.

**v1 ships a skills corpus and an MCP server.** 19 domain and interaction skill files
against v2's registry-with-no-corpus, and an MCP wrapper that makes v1 reachable by agents
that cannot run a CLI. Both are real gaps.

**The isolated world may be a footprint.** v1 injects nothing — no
`addScriptToEvaluateOnNewDocument`, no `createIsolatedWorld`, no `MutationObserver`, zero
matches in its source. v2 installs a persistent runtime into every document and keeps an
observer running. In one paired benchmark cell, on the same task at the same moment with
near-identical work (31 vs 32 commands, within 1.5s), v2 was stopped by a Cloudflare
human-verification wall that v1 walked through. That is n=1 and unconfirmed — the isolated
world is meant to be invisible to page script — but it was the leading suspect.

That suspicion is now dead: `vercel-labs/agent-browser` injects the same four primitives,
so injection is what a serious harness does rather than something v2 does uniquely
(`docs/benchmarks/agent-browser-review-2026-08-29.md`). The observation stands and its
explanation does not. `tests/live/detectability_check.py` is where it gets settled.

**v1 has a team.** It gets fixes on days nobody touches this.

## Layout

The dependency direction is the invariant that makes "core" measurable:

```
harness/       11,182   browser primitives; no domain knowledge
applications/   1,666   job-application workflow, field ontology  ─┐ depend on harness,
evidence/       1,647   recordings, screencasts, bench, telemetry ─┘ never the reverse
```

Both optional layers install themselves into a script namespace; core never imports them by
name. `bh stats`, `bench`, `trace`, `recordings` and `video` are still `bh` verbs, dispatched
outward to `evidence` if it is present and absent if it is not.

## Reading further

- `docs/DESIGN.md` — the decisions, each measured or cited
- `docs/benchmarks/` — run reports, including what did *not* work
- `docs/skills-plugin-system.md` — site knowledge as versioned data, not pull requests
- `TODO.md` — the build plan, with corrections kept next to the claims they correct

Status: active development.

**Known-red:** two `tests/unit/test_field_ontology.py` cases fail on a clean checkout.
`required.txt` was untracked in `06cb421`, and they depend on the populated
`rules.APPLICANT` it provided; they pass only where that file still exists locally. The fix
is for those two tests to supply their own profile, as the neighbouring test already does.
