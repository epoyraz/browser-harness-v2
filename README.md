# browser-harness v2

Agent-facing browser control over the Chrome DevTools Protocol.

Rebuilt from measurements taken against v1 (`docs/DESIGN.md`). The one that set the agenda —
a real 70-page scrape, 1385 results, three ways:

| Mode | Time |
|---|---:|
| Agent loop, one tool call per page | ~17 min |
| One process, navigate + extract | 95.2 s |
| One process, in-page fetch, bounded concurrency | **15.6 s** |

**90% of an agent's wall clock is model decisions; harness primitives are 0.03%.**
So the harness's job is to minimize *decisions*, not milliseconds.

- `docs/DESIGN.md` — 16 decisions, each measured or cited
- `docs/skills-plugin-system.md` — site knowledge as versioned data, not pull requests
- `TODO.md` — the build plan

Status: active development. See `TODO.md`.

Release `0.1.0` adds a hard five-scratch-Chrome budget, a ten-tab ceiling, isolated worker
contexts, cooperative cancellation, concurrency-safe recordings, automatic no-submit
safety, protocol negotiation, and digest-verified path/Git skills. The scheduled real
Chrome gate exercises parallel overlap, cleanup, context isolation, and dry-run blocking.

Action recordings have three explicit profiles: `evidence` keeps one final proof per
high-level action, `review` preserves the established diagnostic frames, and `cinematic`
keeps nested visual beats too. Use `BH_RECORD=evidence|review|cinematic`, or call
`start_recording(profile=...)`. Legacy `BH_RECORD=1` remains `review`; `bh stats` and
`bh bench` report recording wall time, CDP calls, and bytes separately from browser work.

Page reads are versioned semantic blocks: unchanged reads return stable references,
mutations emit only changed blocks, and document-bound cursors fail closed when stale.
All helper results and each invocation's stdout share the `BH_OUTPUT_BYTES` ceiling;
overflow is stored losslessly by SHA-256 and retrieved with `fetch_content(digest)`.
Action helpers carry bounded consequence/validation evidence, navigation grace adapts from
session-local timings while strict mode stays exact, and `fetch_observed_json` can replay
only fully observed anonymous same-origin GET/HEAD JSON endpoints under five explicit caps.
