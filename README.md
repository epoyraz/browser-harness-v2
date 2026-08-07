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
