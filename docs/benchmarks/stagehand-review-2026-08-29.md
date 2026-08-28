# What browserbase/stagehand does that v2 should

2026-08-29. Read the full checkout of `browserbase/stagehand` (24.1k stars, **MIT**,
116,940 TypeScript lines) against v2. Companion to
`agent-browser-review-2026-08-29.md`; where the two agree it is noted, because two
independent implementations choosing the same thing is much stronger evidence than one.

MIT rather than Apache-2.0, so unlike agent-browser this one is genuinely borrowable in
substance. The recommendation is still to borrow the design — v2's position is one
dependency and its own stack — but the licence is not the obstacle here.

## The finding that matters, and it is v2's own thesis shipped as an artifact

```ts
async act(instruction: string,  options?): Promise<ActResult>;   // costs a model call
async act(instruction: Action,  options?): Promise<ActResult>;   // costs none
```

```ts
Action = { selector, description, method?, arguments? }
// "Action object returned by observe and used by act"
```

The loop is:

1. `observe("the submit button")` — a model call resolves it to
   `{selector: "[data-testid='submit-button']", method: "click", description: "..."}`
2. **Cache that object.**
3. Every later run calls `act(cachedAction)` — deterministic, **zero model calls**.

v2's whole argument is *minimise model decisions*, and v2 has no way to spend a decision
once and keep it. Refs (`e7`) die with the document; a `selector + method + arguments`
tuple survives the document, the session, and the process, and can be committed to the
repository beside the script that uses it.

This is the single most transferable idea found in either codebase.

Two details that make it safe, and which v2 would need with it:

- **`selfHeal: boolean`** — when a cached selector no longer resolves, fall back to
  inference rather than failing. Caching without this trades model calls for brittleness.
- **`cache: { threshold?: number }`** — cache only after N hits, so a one-off action never
  becomes a stored artifact.

## Ranked, for v2

### 1. A serializable resolved action, and a cache for it

As above. v2 would need a small typed record — selector, method, arguments, a description
for the human reading the diff — plus `selfHeal` and a hit threshold. `find()` and
`extract()` already return refs and rows; what is missing is a form of the answer that
outlives the page.

### 2. Token accounting on every result

```ts
StagehandResultUsage = { inputTokens, outputTokens, reasoningTokens, cachedInputTokens }
```

Every `act`/`observe`/`extract` result carries what it cost, cached input counted
separately. **v2 has zero token accounting** — verified, `0` files under `harness/`
mention it — because v2 makes no model calls itself.

That is the wrong reason to lack it. A harness whose stated purpose is *fewer model
calls* cannot currently report a single one. The journal counts CDP round trips
precisely; it should be able to record what a caller spent on a decision, so a run's
cost is attributable to the helper that made it necessary. Today the only place that
number exists is the benchmark adapter, which is outside the harness entirely.

### 3. A hybrid a11y + DOM snapshot, frame-aware

`packages/extension/understudy/a11y/snapshot/capture.ts` builds a `HybridSnapshot` from
`a11yForFrame` plus `FrameDomMaps` and a `SessionDomIndex`, resolved across frames by
`FrameSelectorResolver`.

This is the **third independent vote for the accessibility tree** — agent-browser uses
`Accessibility.getFullAXTree`, Playwright-MCP uses Playwright's a11y snapshot — but it is
also a correction to the first two: stagehand does not use the AX tree alone. It keeps DOM
maps alongside it and reconciles them.

For v2 that is the more useful lesson. `SNAPSHOT_JS` hand-rolls the accessible-name chain
(`aria-label || innerText || value || placeholder || name`, documented by `name_source`);
the AX tree would give the computed name correctly, but a pure AX snapshot loses what the
DOM knows. Hybrid is the shape to prototype.

### 4. One protocol schema, many surfaces

`packages/protocol/schemas.ts` is 2,184 lines of zod with `.meta({ id, description,
example })` on every field, and `sdk-ts` / `sdk-python` / `sdk-go` are all built against
it. The descriptions and examples in the schema are what the docs and the tool definitions
are generated from.

v2 hand-maintains its namespace in `session.py`, its help text in `cli/main.py`, and its
agent-facing description in `SKILL.md` — three places, no shared source, already observed
drifting. agent-browser has the same problem and solves it with a checklist in `AGENTS.md`;
stagehand solves it with a schema. The schema is the better answer, and it is also what an
MCP surface would be generated from.

### 5. `domSettleTimeoutMs` as a first-class setting

An explicit, per-instance DOM-settle budget. v2 has `NAVIGATION_GRACE_DEFAULT`,
`NAVIGATION_QUIET`, `NAVIGATION_STABLE` and an adaptive grace, all internal constants with
env overrides. Naming one settle budget in the public surface is clearer than four private
ones, and the 2026-08-28 telemetry says navigation is 58% of an application run's helper
time — this is the knob callers most want.

### 6. An eval harness that compares tools, not versions

`packages/evals/core/tools/` holds adapters for `playwright_mcp`, `chrome_devtools_mcp`,
`playwright_code`, `cdp_code`, `browse_cli`, `stagehand_code`, `understudy_code` — plus a
`claudeCodeToolAdapter` and Braintrust reporting.

We built the same shape for v1-vs-v2 (`benchmark/harness_benchmark/`, and the `bh-harness`
adapter). Theirs compares against *competitors* rather than against its own previous
version, which is the comparison that actually informs a roadmap. Ours could gain adapters
for playwright-mcp and agent-browser cheaply, and that would answer a question v1-vs-v2
never can.

### 7. Caching as a server-side, per-request override

`cache` is set at init and overridable per request. Worth noting mainly as the shape:
a global default with a per-call escape hatch, rather than an env var.

## What the hosted product sells, and why it sharpens item 1

`browserbase.com/stagehand` states plainly what the paid layer adds over running the OSS
repo yourself:

> *"Browserbase gives Stagehand headless browsers with Agent Identity, action caching,
> session replay, prompt observability, and captcha solving."*

Plus zero-infrastructure deployment and cloud browsers. No pricing or performance numbers
are published on that page.

**Action caching is a paid feature.** The schema confirms it from the other side — the
`cache` field's own description reads *"Server-side caching of act/observe/extract results
for this instance… **Requires a Browserbase apiKey and browser sessionId.**"* The
mechanism is in the open-source protocol; the storage that makes it useful is not.

That is the strongest possible endorsement of item 1. The thing this review ranked first
for v2 is the thing Browserbase chose to monetise. And a resolved
`{selector, method, arguments}` needs no server: v2 can write it to a file next to the
script, version it in git, and diff it in review — which is *better* than a remote cache,
not a cheaper substitute for one.

Two more of their paid features are things v2 already has locally and free: **session
replay** and **prompt observability** are the journal plus `evidence/`'s recordings and
screencast. v2 does not charge for them and does not need an account to produce them.
Worth saying out loud in v2's own README, because it is a real position and it is
currently unstated.

The two that are genuinely theirs: **Agent Identity** (stealth and authenticated identity)
and **captcha solving**. Both sit exactly on the axis where v2 lost the one benchmark cell
it lost — a Cloudflare human-verification wall — and neither is something v2 should build.
If that class of site matters, the answer is a `browser.provider` plugin
(`agent-browser-review-2026-08-29.md`, item 8), not a stealth effort in core.

## Where the two reviews agree

| idea | agent-browser | stagehand | v2 |
| --- | --- | --- | --- |
| accessibility tree for element identity | `getFullAXTree` | hybrid a11y + DOM | hand-rolled name chain |
| a versioned schema as the source of truth | `agent-browser.schema.json` | `protocol/schemas.ts` | three hand-kept surfaces |
| model-cost visible to the caller | — | per-result usage | none |
| MCP as a first-class surface | `mcp.rs`, parity enforced | SDKs from one schema | none |

Two independent implementations put the accessibility tree at the centre of element
identity. That is the strongest single signal from either review, and it is the one
architectural change in v2 that nothing else on either list depends on being done first.

## Not taken

Stagehand is an SDK for *building agents*: `act`/`observe`/`extract` take natural-language
instructions and make model calls inside the library. That is the boundary v2 deliberately
put between `harness/` and `applications/`, and adopting it would undo the split this
codebase spent three stages establishing. What transfers is the **artifact** their model
calls produce — the cached `Action` — not the calls.
