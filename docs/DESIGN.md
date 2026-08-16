# Browser Harness v2 — Design

**Status:** draft for a fork we control.
**Method:** every claim below is either measured on real Chrome (151, macOS, non-headless,
attached to a live user profile) or cited to an issue/PR in `browser-use/browser-harness`.
Numbers without a citation were measured on 2026-08-05; see `§10 Reproducing the measurements`.

**Companion documents**
- [`docs/skills-plugin-system.md`](docs/skills-plugin-system.md) — full specification for D6:
  sources, index schema, match resolution, trust tiers, lockfile, CLI, migration.
- [`docs/v2-architecture.html`](docs/v2-architecture.html) — interactive project structure:
  per-module verdicts, layer diagram, v1→v2 tree, line budget.

---

## 1. The one number that sets the agenda

Running a real task — collect every "Software Engineering" listing on jobs.ch, 70 pages,
1385 jobs — the way an agent naturally does it, one tool call per page:

| Component | Per step | Share |
|---|---:|---:|
| Model decision (gap between tool calls) | 13.1 s | **90.0%** |
| Page load | 1.29 s | 8.8% |
| CLI process startup | 134 ms | 0.9% |
| **Harness primitives** | **~5 ms** | **0.03%** |

Same task, three ways, identical 1385 results:

| Mode | Time |
|---|---:|
| Agent loop, one tool call per page | ~17 min (70 × measured step cost) |
| One process, navigate + extract ×70 | 95.2 s |
| One process, in-page fetch, 6 concurrent | 15.6 s |

**A 65× spread, and none of it comes from making primitives faster.**

> **Design principle 0 — the harness's job is to minimize *decisions*, not milliseconds.**
> A helper earns its place by collapsing steps. Optimizing a 2 ms primitive is noise;
> removing one round trip is worth 13 seconds. Every decision below is judged against this.

Primitive costs, for calibration (median, real Chrome, 450-element page):

```
ipc_ping 0.71ms   js() 0.84ms   page_text 0.78ms   wait_for_element 0.61ms
click_at_xy 1.67ms   press_key 2.17ms   cdp() 2.70ms   page_info 3.06ms
snapshot(450 els) 8.55ms   scroll 16.5ms   goto+wait 123ms (local)
fill_input(20 chars) 174ms   capture_screenshot 194ms
```

Only two primitives are expensive, and both are addressed in §4.

---

## 2. What the corpus says

213 open PRs, 228 merged, ~81 closed-unmerged, 50 issues — repo is four months old.

| Open PRs | Count |
|---|---:|
| domain-skills (site content) | **109 (51%)** |
| **core code** | **48** |
| other/config | 24 |
| interaction-skills | 17 |
| docs | 15 |

Median open-PR age **55 days**; oldest 108. The queue is not being *rejected* — it is
being *unreviewed*.

**Recurring clusters among the 48 core PRs.** When N independent people write the same
fix, that is one design flaw, not N bugs:

| Cluster | PRs |
|---|---|
| session / tab pinning & cleanup | 11 — #208 #346 #347 #353 #393 #402 #417 #455 #478 #526 #562 |
| `press_key` / `fill_input` char duplication | 5 — #297 #332 #346 #421 #570 |
| Windows / platform | 6 — #287 #333 #374 #431 #432 #433 |
| timeout / reconnect | 3 — #347 #384 #523 |
| stop stealing OS focus | 4 — #402 #498 #504 #513 |

### 2.1 What already shipped, and what it cost

Of 228 merged PRs, only **89 (39%)** touch a core `.py` module; 113 touch only prose.
File churn concentrates hard: `daemon.py` **38 PRs**, `helpers.py` **37**, `admin.py` **27**
(highest lines-per-PR, and it never touches a page), `_ipc.py` **11 PRs for 212 LOC** — an
extreme churn-per-line ratio for a file whose job is "open a socket."

**Recurring regressions — the same bug class fixed repeatedly.** These, not the open PRs,
are the design flaws:

| # | Class | PRs | Root cause |
|---|---|---|---|
| A | Two divergent session-establishment paths | 4 — #70 #234 #296 #305 | A fresh CDP session starts with **all domains disabled**. `attach_first_page()` knew this; `set_session` did not — so every `switch_tab()` silently dropped Network events and broke `wait_for_network_idle()` four days after it shipped. |
| B | `DevToolsActivePort` vs `/json/version` | 6 — #17 #173 #260 **#265** #292 #548 | Neither source of truth is reliable. #265 is a **direct reversal** of #260. `get_ws_url()` is now a 78-line fallback ladder for a function that returns a string. |
| C | "Allow remote debugging" popup | 6 — #67 #161 **#204** #232 #548 #560 | Oscillated between retry and don't-retry. The popup is **per-connection**, so connection count — not retry count — is the variable to minimize. |
| D | Windows IPC follow-ups to one fork (#225) | 8 — #240 #241 #243 #244 #276 #309 #318 #496 | One platform fork spawned eight follow-ups, incl. `os.kill(pid,0)` raising `SystemError` not `OSError`, and a daemon that self-crashed writing its own log under a non-UTF-8 locale. |
| E | `js()` return detection | 4 — #187 #199 #230 #231 | A `"return " in expression` substring check standing in for a parser; double-wrapped IIFEs silently returned `None`. |
| F | Cloud auto-spawn gating | 3 — #266 #277 #300 | `BROWSER_USE_API_KEY` alone triggered a **billed** browser; then it silently overwrote an explicit `BU_CDP_URL`. Money on the line, three passes. |
| G | Exec transport | 4 — #188 #211 #229 #343 | heredoc → `-c` → back to heredoc. Ended exactly where it began. |
| H | Controlled-tab title marker | 4 — #69 #70 #171 #322 | A **decoration** that cost a 4× latency regression: awaiting `Runtime.evaluate` on both load events serialized behind cdp-use's dispatch — median iteration **4.595 s → 1.060 s** once made fire-and-forget (Playwright baseline ~0.9 s). |
| I | Daemon health-probe semantics | 6 — #161 #240 #254 #276 #294 #548 | There has never been a single agreed definition of "the daemon is alive and usable." A `{"meta":"session"}` probe answers from a cached Python dict and reports healthy on a **dead** daemon. |

**v2's job is to make each class unrepresentable**, not to merge its fixes.

### 2.2 What the commit history shows that PRs cannot

**Most work never goes through a PR.** 481 commits: 147 merges, **254 non-merge commits
with no PR reference** — more than half of all work is pushed straight to main by the core
team (Magnus Müller 50 + 23, Gregor Žunič 42, Alezander9 33, Saurav Panda 32 + 14, Laith
Weinberger 22). This reframes §2 entirely: **the 213-PR backlog is not a blocked pipeline,
it is a mostly-ignored side channel.** Maintainers are not stalled; they are working
elsewhere. A fork inherits none of that funnel, which is a feature.

**The core tripled in four months.**

| Release | Core LOC | Modules |
|---|---:|---:|
| v0.1.4 | 2,978 | 9 |
| v0.1.5 | 3,296 | 9 |
| **v0.1.6** | **4,933** | **12** |
| v0.1.7 | 5,016 | 12 |
| v0.1.8 | 5,302 | 12 |

**Module birth dates explain the whole shape:**

```
2026-04-16  daemon.py  helpers.py  run.py     ← the original harness: 3 files
2026-04-17  admin.py                          ← day 2; now the largest file (1,056 LOC)
2026-04-27  _ipc.py                           ← the Windows transport fork
2026-06-20  auth.py  telemetry.py  paths.py   ← the product layer, one day, +1,637 LOC
2026-07-14  recorder.py
2026-07-16  video.py  video_render.py         ← +2,780 LOC, merged with no tests
```

The browser harness proper is three files from day one and has been roughly stable. Growth
came in two bursts — commercial plumbing on 2026-06-20 and video production in mid-July —
**and neither touches a page.** `admin.py` appearing on day 2 and becoming the biggest
module is the clearest signal in the repo that *connection lifecycle*, not browser control,
is where the complexity actually lives (D8, D10, D11 all target it).

**Test coverage moved backwards on the newest subsystems.** `test_recorder.py` and
`test_video_skill.py` were deleted 2026-07-16 (same day the video modules landed);
`test_telemetry.py` was deleted 2026-08-02. The 2,780-line video subsystem shipped in one
PR with no tests, and what tests existed were then removed.

**19 explicitly subtractive commits** (`simplify:`, `Trim`, `Remove`, `drop 14 unused
helpers`, `Slim ipc.py`) confirm the smallest-diff doctrine is real — but they are almost
entirely docs and skills. Code grew 78% anyway. **Stated doctrine did not constrain code
size; only architecture can.**

---

## 3. Architecture decisions

### D0 — Collapse N decisions into 1 wherever the plan is knowable

Design Principle 0 says minimize decisions. This is *how*, and it is the single largest
source of speed in the document — every other decision here is worth milliseconds by
comparison.

**The architectural reason it is possible at all: the harness runs programs, not tool
calls.** An agent writing Python over CDP can express a loop; a tool-call surface must round
trip once per iteration. **Every `for` loop the agent writes is N decisions it did not
spend** — that is the difference between the top and bottom rows of §1.

That is a **trade, not a strict win**, and the trade is the reason tool-call harnesses
exist at all:

| Tool calls buy | Because |
|---|---|
| **Gating** | a typed, discrete, inspectable action can be intercepted before it runs — *"the agent wants to click Submit; ask the user."* Nobody can meaningfully approve `exec(script)`. Every human-in-the-loop design depends on actions being enumerable in advance, which is exactly why v1 ships no permission system and says containerize instead. |
| **Failure granularity** | a failed call names the step and leaves state intact; a script dying at line 60 returns a traceback and murky partial progress |
| **Interruptibility** | a seam between steps; a long script is effectively atomic |
| **Model alignment** | models are post-trained on tool-call harnesses — ~20% malformed calls under schema drift, and measured "drastic performance drops under tool-environment shift" |
| **Portability** | MCP tools run anywhere; a Python REPL needs Python plus this harness |

And the cost of batching is not zero: **it converts *walk-the-loop* decisions into
*debug-the-batch* decisions.** Building the measurements in this document, scripts failed
roughly ten times — top-level `await`, a shadowed `URL` constructor, rest.li encoding
returning HTTP 400, `$id` vs `entityUrn` keying, an unbounded fan-out that silently dropped
45% of results, a `#` truncating a data URL, ref misalignment, zsh's `path` clobbering
`PATH`, `'ort' ⊂ 'jobportal_taca'`, and a URL needing a slug. Each cost a full decision, and
several failed **silently**, which a discrete call would have surfaced at once.

**The numbers below are the successful run; they do not price the path to a correct
script.** A wrong batch costs what ten mostly-working tool calls cost.

So the choice is a spectrum with three conditions:

| Condition | Favours |
|---|---|
| the plan is knowable before you look | program |
| the action needs gating, or is hard to reverse | tool call |
| a wrong batch is expensive, or fails silently | tool call |
| the loop is mechanical and cheaply verifiable | program |

This matches the one benchmark in the literature that tested it directly: bash-only scored
**52.7%** on a structured-query task against a dedicated operation at **100%** with 7× fewer
tokens — and the **hybrid also reached 100%**. Dedicated operations on the hot path, general
scripting for the long tail. D15's `form_schema` / `fill_form` are exactly that hybrid: a
purpose-built operation for the repeated case, with `js()` underneath for everything else.

Three measured instances of the same move, all from real tasks:

| Task | Walked | Collapsed | Gain |
|---|---:|---:|---|
| Paginate 70 jobs.ch pages, 1385 results | 70 decisions, ~17 min | 1 decision, 15.6 s | **65×** |
| Fill 19 fields across 4 live ATS forms | 19 decisions, 965 ms | 4 decisions, 28 ms | **34×**, 137× fewer round trips |
| Read 7 candidate job pages | 7 decisions, 10.8 s, 35 CDP | 1 decision, 862 ms, **1 CDP** | **12.5×**, 7/7 titles recovered |

The patterns, in order of leverage:

1. **Fan out over candidates rather than walking them.** N URLs, same extraction from each:
   fetch them from inside the page with bounded concurrency and return all digests at once.
   The trade is wasted work on candidates you discard — spending *milliseconds* to save
   *decisions* at a 13,100:1 ratio is always correct.
2. **Express the task as a program.** Loops, retries and conditionals belong in the script,
   not in the agent's turn-taking.
3. **Make the default return rich.** One page digest is 5 ms and ~293 tokens and answers
   what three targeted queries answered. A narrow selector is a guess, and a missed guess
   costs a whole decision — which is why raw-HTML-plus-grep consistently beat targeted
   `js()` during this investigation.
4. **Speculate on both branches.** When the next step depends on unpredictable content,
   fetch both and decide once. One jobs.ch API call returned every apply URL and replaced
   seven navigate–look–decide cycles.
5. **Checkpoint instead of re-deciding.** A run that dies at item 47 of 100 must not
   re-derive the first 46.

**Where it cannot help**, and what does: genuinely novel judgement on unfamiliar content —
*is this the right posting, is this actually an application form* — is irreducible. D6's
recipe cache removes it after the first encounter, and (3) makes each remaining judgement
better-informed rather than requiring a follow-up look.

> The honest asymmetry from building this: mechanical loops get batched instinctively.
> **The decisions actually burned were in discovery** — 11 of them to locate one job
> posting, against 8 ms for the form fill that was the task. Optimising the known path is
> nearly free; the remaining win is in the unknown one, which is why (1) and (3) rank above
> further form work.

**Where this leaves the design.** Keep the program surface — it is what makes the 65×
possible and it cannot be retrofitted onto a fixed tool list. But do not treat it as
strictly superior: the gating property is real and v1 answers it only by telling users to
containerize. If v2 ever needs human-in-the-loop approval, it will need a *declarable*
action layer above the REPL — actions the harness can enumerate and gate — rather than
inspecting arbitrary code. That is an open question (§9), not a solved one.

### D1 — One session per target, never pooled

**Today:** `Daemon` holds a single `self.session` / `self.target_id`. Every call routes
through `req.get("session_id") or self.session`, and `switch_tab()` mutates it globally.

**Consequence:** two clients sharing a daemon overwrite each other's notion of "the current
tab" (issue #375 — subagents fighting over tabs).

**v2:** the daemon keeps `{targetId → sessionId}`, attaches lazily on first use, and routes
every request by target. A client pins a target for its lifetime. This is what Puppeteer
(`CDPSession` per Target) and Playwright (`connectOverCDP`) have always done, and it is the
design in **#119**.

**Measured, today, on Chrome 151:**
- Two `Target.attachToTarget(flatten=True)` sessions over **one** websocket drive two tabs
  concurrently. No second port, no second browser, no cloud browser needed.
- And the alternative is not merely worse but unusable: a second connection to an
  already-authorised Chrome is **denied a fresh consent prompt every time** (D7), so
  multiplexing is the only design that scales past one client.
- A **backgrounded** tab accepts synthetic mouse input addressed to its session
  (`click B while A is foreground → B's handler fires, A untouched`).

**Correction to #119's stated rationale:** its repro — user clicks another tab, session
silently drifts — **no longer reproduces** on current main with Chrome 151 (verified
2026-08-05; `js()`, `page_info()`, `current_tab()` all stayed pinned). That answers the
maintainer question that stalled it. Adopt the design for **concurrency**, not for drift.

**Hard requirement from regression class A:** there must be **exactly one function that
turns a `targetId` into a ready session**, and every path — initial attach, tab switch,
lazy attach on first use — goes through it. v1 had two, and the one that knew a fresh CDP
session starts with all domains disabled was not the one `switch_tab()` used. That single
divergence produced four separate bugs over four PRs, including silently breaking
`wait_for_network_idle()` four days after it shipped.

**The invariant beneath all of this, stated so nobody "simplifies" it away: a target id
is IDENTITY; a session id is a LEASE.** Callers — `Tab`, scripts, the daemon's clients —
hold target ids and nothing else; the session id is re-resolved on every call through
`ensure_live()`, never stored across calls, never handed to a caller as a thing to keep.
Two consequences do a lot of quiet work:

- *Recovery is trivial and cannot redirect.* When the browser detaches a session
  (`SESSION_STALE`), the tab is usually fine — a lease expired, not the thing it named —
  so `ensure_live` takes a new lease on the **same target** and the caller never notices.
  Silent redirection to the wrong tab is structurally impossible: the replacement is for
  the target the caller named, by construction. Compare browser-use PR 618, which fixes
  the v1-shaped version of this: because v1 callers hold *session* ids, its recovery
  needs a session-replacement map with chain preservation, a cap, and an explicit rule
  that named-session callers must get an error rather than a redirect. None of that
  machinery has a counterpart here — this one sentence is why. The states that stay
  fatal stay fatal: a destroyed target has nothing to re-attach to, and a crashed
  renderer needs a reload decision the caller owns.
- *Per-session state must follow the lease.* Injected-script registrations (`SAFETY_JS`,
  the isolated-world runtime) and the wait binding live on the session and die with it,
  announcing nothing. `Tab._sid()` watches the lease change and re-arms them before the
  noticing call proceeds — the dry-run guard's presence on the next document must never
  depend on which lease happened to register it.

**Correction to the background-input measurement above (2026-08-16, Windows Chrome):**
"a backgrounded tab accepts synthetic mouse input addressed to its session" does **not**
hold on Windows. Measured with page-side listeners: the renderer silently drops raw
`Input.dispatchMouseEvent` *and* `dispatchKeyEvent` for any tab that is not its window's
selected tab (0 of N events reached the page's handlers; the CDP call ACKs regardless),
and `mouseWheel` never ACKs at all. `Input.insertText` and DOM-level dispatch survive.
v2 therefore does not rest on the original claim: every raw-input path verifies delivery
against isolated-world counters (`__bh.keys` / `__bh.scrolls` / the click delta) and
falls back through the DOM when provably nothing arrived — see `type_chars`,
`press_key`, `scroll`, and `_activate_click` in `ops/page.py`.

### D2 — Never steal OS focus

Four PRs exist to remove focus stealing (#402 #498 #504 #513). D1's measurement shows why
it was never needed: background tabs receive input fine when addressed by session.

Focus stealing is not merely unnecessary — with N concurrent clients it is *actively
harmful*, since every click yanks the window between tabs. **v2 never calls
`Target.activateTarget` implicitly.** The explicit opt-in is `Session.activate_tab()`
(2026-08-16, mirroring browser-use PR 618's attach/activate split): it exists for a page
that demonstrably pauses visibility-dependent work while hidden, and for a human who
wants to watch — never for input, which the delivery-verified fallbacks in D1's
correction handle without it.

> Caveat to carry forward: backgrounded tabs pause `requestAnimationFrame` (#511), so
> *screenshots* of a background tab can be stale even though input works. Screenshot is the
> one operation that may need foregrounding — make that explicit and opt-in, never a
> side effect of clicking.

⚠️ **Evidence hygiene note.** A research pass over this repo cited
`helpers.py:162-171` — *"Chrome hit-tests mouse/key events against the composited
(foreground) surface … events dispatched into a background tab are silently swallowed"* —
as an established project design principle justifying focus stealing. **That comment is not
project doctrine. It is an uncommitted change written on 2026-08-05 during this
investigation** (`git show HEAD:…helpers.py` does not contain it), and it is **wrong** — the
two-session experiment above disproves it directly. It has since been retracted.
Any v2 research that reads a dirty working tree must separate `HEAD` from local edits, or
it will launder a fresh mistake into a design constraint. The rest of D2 rests on the
measurement, not on that comment.

### D3 — Text entry defaults to one round trip

`fill_input` dispatches three CDP events per character: **8.7 ms/char**, so 174 ms for 20
characters and ~17 s for a 2000-character cover letter. Five PRs address character
duplication in this path — the per-key design is the bug generator.

**v2:** set value + dispatch `input`/`change` as the default; keystroke-level entry is an
explicit opt-in for the minority of widgets (location/school autocompletes) that need real
key events.

### D4 — Screenshots at CSS-pixel resolution, JPEG by default

`Page.captureScreenshot`'s `clip.scale` is relative to a fixed 2× baseline and **ignores
`devicePixelRatio`**, so `scale=0.5` yields exactly one image pixel per CSS pixel on any
display — the space `click_at_xy` consumes. No DPR math, no coordinate extrapolation.

| Variant | Time | Output | ~Image tokens |
|---|---:|---|---:|
| png, no clip (v1 default) | 314 ms | 3584×1704 | ~8,140 |
| **jpeg q70, clip 0.5** | **197 ms** | 2240×1065 | **~3,180** |
| jpeg q70, clip 0.35 | 102 ms | 1568×746 | ~1,560 |
| webp q80 | **703 ms** | — | — |
| v1 `max_dim=1400` (PIL post-resize) | **584 ms** | 1400×666 | ~1,240 |

WebP is a trap (2.2× slower than PNG). v1's `max_dim` nearly doubles wall time to save
tokens; CDP-side scaling does both. Below CSS resolution, coordinate error scales
inversely — halve resolution, double the miss distance.

> Prefer `snapshot()` (8.6 ms, exact coordinates from `getBoundingClientRect`) over a
> screenshot (194 ms + thousands of image tokens + an estimated coordinate) for locating
> anything the DOM can describe. Vision is for *which*, the DOM is for *where*.

### D5 — Transport: one CDP websocket, per-request session routing

Keep the daemon-holds-one-websocket model. It is why primitives cost 0.7–2.7 ms, and on
local Chrome the single held connection is what makes the Chrome 144+ permission popup a
one-time click rather than per-attach.

**Position on WebDriver BiDi (#564, +3362, draft):** not in v1 of the fork. It buys
cross-engine support (Firefox), which is not a goal here, at the cost of a second protocol
adapter across every helper. Revisit only if a target site is Firefox-only.

**Rejected — request batching.** Chrome DevTools MCP and Claude in Chrome both ship one.
Measured: IPC 0.71 ms, CDP 2.70 ms — batching five calls saves ~10 ms against a 13-second
decision. Pure ceremony. (Recorded here so it does not get proposed again.)

**Rejected — blocking subresources to speed navigation.** `Network.setBlockedURLs` with
images/fonts/CSS blocked on jobs.ch: 1973 ms → 1819 ms, **1.08×**, inside the noise. The
time is in the document request and JS execution.

> Gotcha worth encoding: `Fetch.enable` with no handler pauses every request and wedges the
> browser. `setBlockedURLs` is the safe primitive.

### D6 — Skills are data, and get a package manager

51% of open PRs are site recipes queued for human review behind daemon code. That is why
core fixes wait 55 days. The root cause is a **lifecycle mismatch**: code wants review,
versioning and backward compatibility; site knowledge wants freshness and volume. Sites
change daily, a library releases monthly, and coupling them makes review bandwidth the
bottleneck for something that should never have needed review.

Today this is a directory, not a system: `domain-skills/<first-hostname-label>/*.md`,
resolved by one line in `goto_url()` returning up to ten filenames, with **2 of 105 files
carrying any machine-readable metadata**.

**v2 borrows four pieces from package managers** — `source` (ordered, trust-tiered, like an
apt `sources.list` or a Homebrew tap), `index` (small manifest: id, match rules, version,
digest — never bodies), `body` (fetched on match, content-addressed, cached), and `lock`
(id → version + digest, for reproducible runs).

**Resolution is triggered by a page, not a name** — the one thing no package manager does.
`npm` answers "give me lodash"; this answers "what applies to *this URL*?", closer to how
an ad-blocker picks filter lists. Skills declare match rules; predicates run cheapest-first:

| Tier | Predicate | Cost | Needs a page? |
|---|---|---|---|
| 1 | `host` glob | free | no |
| 2 | `url` regex | free | no |
| 3 | `detect` CSS selector | one *batched* `js()` | yes |

So `match(url)` costs zero browser round trips and is free to call on every navigation.
This is also what retires the multi-tenant bug: `acme.jobs.personio.de` and
`bravo.jobs.personio.de` both hit one `personio/apply`, and a company with a customised
form ships `acme/apply` at higher priority without touching the shared skill. Layering by
priority replaces one-directory-per-hostname-label.

**Trust tiers are what make the split viable.** A skill is *instructions to a model driving
a logged-in browser*, so a public index is a prompt-injection surface with real credentials
behind it — this project has already met that: PR #454 is blocked to this day by an
automated review flagging a skill file as `malicious_code`, with *"Skill authorship is
restricted to maintainers."* `owner` and `team` sources reach the model as instructions;
`public` reaches it as delimited, explicitly-labelled reference material with no authority.
**Because public skills carry no authority, they need no human review** — CI schema
validation replaces the maintainer, and that is the only honest way to let site knowledge
scale. Bodies are digest-verified on every load; a mismatch is a hard failure.

Library surface stays at roughly a hundred lines and ships no content:

```
skills.match(url, page=False) -> [SkillRef]   # index only; no network, no CDP
skills.load(ref)              -> SkillBody    # cached, digest-verified
skills.record(ref, ok, note)                  # local outcome feedback
skills.write(id, body)                        # → workspace source, instant
```

That last call closes the loop #145 correctly said v1 never had: read → attempt → record →
write back locally (free, no review) → promote deliberately. Decay becomes explicit —
`verified_at`, local failure counters, automatic deprioritisation — which a curated in-tree
catalogue cannot do. #159 was closed with *"Spirit just went out of business today"*, and
nothing in the repo could have known.

**Migration payoff is immediate:** generate frontmatter for the 105 existing files from
path + first heading, publish them as the first `community` source, drop the directory from
the release, and redirect the 109 open skill PRs to a repo where CI validates them and no
maintainer reads them. **51% of the open-PR queue stops needing human attention.**

Deliberately excluded: no dependency graph between skills (that needs a solver, and solvers
are how package managers become a career), no executable plugins (a skill is markdown; code
goes in the already-editable `workspace/helpers.py`), and no central registry to start —
`path` and `git` sources cover the real cases.

→ Full specification, including schemas, source config, lockfile and CLI:
[`docs/skills-plugin-system.md`](docs/skills-plugin-system.md).
### D7 — Minimize *connection count*, not retry count

Chrome 144+ prompts on **every** CDP connection. v1 oscillated — 12 retries (12 stacked
popups) → 1 try → re-add retry → retry floods a new `chrome://inspect` tab every 7 s →
finally hold one 45 s handshake (regression class C, 6 PRs).

The stable formulation: **each connection costs a user interaction, so connections are the
scarce resource.** One held connection, patiently. Never a retry loop. This is also why
D5's single-websocket model is load-bearing rather than merely tidy — and why D1's
"N sessions over one connection" is the only concurrency design that doesn't multiply
consent prompts.

#### Measured: consent is per *connection*, not per browser instance

This was the one assumption the whole concurrency design rested on, so it was tested rather
than inherited. Against a Chrome **already authorised** and actively driving this session,
six fresh websockets were opened to the browser endpoint:

```
sequential  0/3 succeeded   — TimeoutError during opening handshake, ~8000 ms each
concurrent  0/3 succeeded   — TimeoutError during opening handshake, ~8000 ms each
```

Every one stalled on a **new** consent prompt (*"An external app wants full control over
this Chrome session to debug it… access to your saved data, cookies and site data"*), while
the daemon's existing connection kept working throughout. Prior authorisation buys nothing
for the next connection.

Two consequences, one of them not obvious:

- **Per-client daemons are not viable on local Chrome.** N clients would put a modal between
  the user and every subagent — issue #375's 20-parallel-agent case becomes 20 prompts
  before any work begins.
- **Chrome queues one sheet for six connections**, so the prompts are *serialised*. N daemons
  cost N sequential clicks, each blocking a handshake for up to the 45 s timeout — the cost
  is not N × one click, it is N clicks in series.

This retroactively justifies v1's strangest-looking constant, `LOCAL_HANDSHAKE_TIMEOUT = 45`.
It reads as defensive padding and is in fact the only workable shape.

### D8 — WS-URL discovery cannot assume the filesystem

**The most expensive false assumption in v1: "a CDP WebSocket URL can be discovered from
the filesystem."** Chrome has broken it three separate ways — M136 silently ignores
`--remote-debugging-port` against the default `--user-data-dir`; M144 gates every
connection behind consent; M147 404s `/json/version`, `/json`, and `/json/list` on the
default profile. Each break was absorbed as another branch, and `get_ws_url()` is now 78
lines of fallback ladder (regression class B, including a direct reversal).

**v2:** treat endpoint discovery as a first-class, explicitly-ranked strategy list with one
declared source of truth per strategy and structured failures — not a nested ladder. Make
the explicit path (`BU_CDP_URL`-equivalent, launched on a known port and profile) the
*documented default* for anything automated, and treat "discover the user's running Chrome"
as the convenience path that is allowed to fail with a clear instruction.

Related: `PROFILES` is **30 hardcoded paths** across three OS tuples and grows every time a
Chromium fork ships (Brave, Arc, Comet, Dia, Helium, Flatpak ×4, Edge ×4…). There is no
discovery mechanism, only a list. v2 should enumerate candidate profiles from the
filesystem plus an override, not hardcode vendors.

### D9 — Nothing decorative in the hot path

The 🐴 tab marker took 4 PRs and caused a **4× latency regression** — median iteration
4.595 s → 1.060 s once the `Runtime.evaluate` on both load events stopped being awaited
(#171; Playwright baseline ~0.9 s). It also mutates the page being observed: **every
scraped title carries the emoji** (verified — `page_info()` returns
`'🐴 1391 Software Engineering jobs'` where `og:title` is clean).

**v2:** the harness does not write to the page. If a "which tab is the agent driving"
affordance is wanted, it belongs outside the document — and it is never awaited on a
navigation event.

### D10 — Endpoint binding is explicit and fails closed

**This is the highest-severity finding in the corpus.** A daemon pinned to a specific
browser, respawned without `BU_CDP_URL` in its environment (and `ensure_daemon()` re-runs on
*every* helper invocation), falls through `get_ws_url()`'s unconditional chain to local
profile scanning and **silently attaches to the user's daily-driver Chrome** — with their
logins. The reporter of #479 calls it *"a real cross-project daily-driver breach."*

Their sharpest observation: it only *looks* safe today because Chrome 136+ gates the
default profile behind the Allow dialog. **v1 fails closed by accident.**

**v2:** a daemon persists an explicit endpoint binding with a declared trust mode —
`pinned` (never widen, ever; refuse and explain) or `discover` (opportunistic). Resolution
enumerates *liveness-probed* candidates rather than taking the first filesystem match, and
scope never widens on respawn. Related open bug: `_is_local_chrome_mode()` checks
`BU_CDP_WS` but not `BU_CDP_URL` (#425), so an explicitly-pinned user still gets the local
Chrome recovery flow — a parallel-code-paths-that-drift bug, of which the corpus has four.

### D11 — One outcome contract: type it, propagate it, record it

*Consolidates what were separate decisions on error classification, tracing, and
robustness. They were three views of one missing abstraction.*

**The system has exactly one error type, and it is `str`.**

```
Chrome/CDP   structured: errorText, exceptionDetails, protocol codes
   ↓
daemon       return {"error": str(e)}          ← all type information dies here
   ↓
wire         {"error": "<english sentence>"}
   ↓
helper       raise RuntimeError(r["error"])    ← one Python type for every failure
   ↓
agent        a traceback
```

Recovery then parses the English back out — `"Session with given id not found" in msg`
(daemon.py), `"Illegal return statement" in str(exc)` (helpers.py), `"permission-blocked"
in lower` (admin.py). That is why every reworded Chrome message is a new bug (#352), and
why regression class B needed six PRs including a direct reversal. A half-built version of
the right idea already exists — the daemon returns bare sentinels `not_attached` and
`cdp_disconnected` — but they travel in the *same string field* as arbitrary prose, so no
caller can tell a class from a message.

#### Three failure modes on one channel

| Mode | What happens | Evidence |
|---|---|---|
| **Invented** | a cause nobody verified is asserted | 9 issues; a handshake timeout reported as "click Allow" even with Chrome running **zero windows** (#554), a datacenter WS rejection (#108), and a *cloud* provisioning failure (#181/#183) |
| **Discarded** | a cause we were handed is dropped | `goto()` receives `errorText: net::ERR_HTTP_RESPONSE_CODE_FAILURE` and ignores it; the daemon logs `Connection lost` and tells no one; a stale-session reattach silently switched to a different tab |
| **Undefined** | no error at all, because success was never specified | `goto()` on a dead URL returns a title and a URL — `chrome-error://chromewebdata/` reads as a successful navigation |
| **Incomplete** | *part* of the work is done and reported as done | an unbounded fan-out returned **163 of ~300** results with no error raised; a batch fill wrote 5 of 8 fields while the form reported `valid` |

Mode 3 is the expensive one. Measured on a real task: finding one job posting cost **~11
agent round trips ≈ 2.4 minutes of model time**, of which **~3 were spent chasing a 404
that reported success** and ~4 more on narrow queries that had to be retried. Seven of
eleven decisions were harness-caused, not task-caused — against 8 ms for the actual work.

#### The contract

An operation returns an **outcome**, not a value, typed at the boundary where the
information still exists:

```json
{"ok": false,
 "class": "navigation_failed",
 "detail": "net::ERR_HTTP_RESPONSE_CODE_FAILURE",
 "observed": {"requested": "https://…/careers",
              "landed": "chrome-error://chromewebdata/"},
 "retryable": false,
 "id": "c7.1"}
```

- **`class`** is a closed enum. Recovery branches on it, never on prose, so Chrome may
  reword freely. Distinct classes for `permission_pending` (requires an *observed* prompt),
  `endpoint_404`, `no_browser_window`, `ws_rejected_upstream`, `target_gone`,
  `session_stale`, `renderer_unresponsive`, `navigation_failed`.
  Two members were added by implementation, each because a caller could now distinguish a
  case it previously could not — the only admissible reason to widen a closed enum:
  `http_error` (a 404 inside `fetch_all` is neither a navigation nor a JS throw) and
  `needs_interaction` (below). One more, `cdp_error`, is the honest floor: an unrecognised
  CDP error must not be reported as some *specific* cause we never verified.
- **`observed`** carries the evidence, so a claim is auditable rather than asserted.
- **`detail`** is for humans and is never parsed.
- **`retryable`** is stated by the party that knows, not guessed by the caller.
- **`id`** correlates client, daemon log, trace and replay — see (c).
- **`ok` is explicit**, so absence-of-exception can never be mistaken for success.

Three rules: **type at the source** (the daemon knows stale-session from dead-socket from
protocol error — classify there, never stringify and re-parse); **never invent, never
discard** (both directions of one channel); **define success** (every operation states its
condition and returns evidence — `goto` succeeds only if the landed URL is not an error
page, and returns *both* requested and landed, which on the four-hop redirect chain
jobs.ch → career.ti8m.com → prospective.ch → abacuscity.ch is load-bearing).

**"Define success" has a sharp edge that only production found.** The obvious definition
for a write is `el.value === want` immediately afterwards. That is wrong for any
framework-controlled input that *normalises*: jobs.ch's React phone field rejects
`079 123 45 67` outright but rewrites `+41791234567` to `+41 79 123 45 67`. The write
succeeded; the check was simply taken too early, and reported a working fill as a failure —
permanently, since the value never becomes byte-identical. Success is therefore **"the
field now holds a value the page accepted"**, verified after a settle, with the comparison
tolerant of reformatting (digits-equal for phone-shaped values). The settle costs *one*
extra evaluate for the whole form, not one per field.

The symmetric error is worse and is covered under D15: a definition of success that a
**decoy element** can satisfy. Both are the same lesson — the *definition* is the design
surface, not the check.

#### Actions return a *delta*, not just the absence of an exception

Some effects cannot be read back, because they are not values. A click's outcome is a
change, so the outcome must describe the change:

```
click_ref(3) → {url_changed: false, new_tab: false, dom_delta: +46 nodes,
                dialog_opened: true, dialog_text: "…Continue without an account"}
```

Cost is one `js()` diff around the action, ~1 ms.

This one was learned the expensive way, and by the operator rather than the code. Clicking
*Apply* on jobs.ch, the harness reported nothing; `location.href` was unchanged and no tab
had opened, so the click was pronounced blocked and the whole path abandoned — four further
calls spent theorising about trusted events and popup blockers, and a confident public claim
that *"jobs.ch resists the handoff."* It was false. The click had worked on the first
attempt and opened a modal offering **"Continue without an account"**; the page went from
759 to 805 nodes and 3,946 to 4,228 characters of text, with `[role=dialog]` count 1. Every
one of those signals was one call away.

The failure is *undefined success* again, from the caller's side: **a narrow predicate was
tested (`did the URL change?`) instead of observing what happened.** A false negative on a
prediction was read as a failed action, and roughly a minute plus a wrong diagnosis followed.

Two things fall out. An action primitive that returns its delta makes this class of mistake
structurally unavailable — and *no operator discipline is required*, which is the point:
**do not blame the caller for a mistake the instrument can prevent.** It also explains why
`fill_form`'s per-field read-back was easy to invent and the click equivalent was not: fills
have a readable value, clicks only have consequences.

In Python this surfaces as typed exceptions carrying the payload —
`except NavigationFailed as e: e.landed` — rather than a string the agent must read.

#### (a) Verification: robust by default

v1 is not robust by default because **the default path is the convenient one and failures
are silent**. The recurring shape is an operation that reports success and does nothing: a
click into a stale coordinate; `js()` returning `None` for a non-serializable value; an
unbounded fetch fan-out silently dropping pages and under-reporting by 45%; `close_tab()`
leaving the daemon on a dead target (#379, still unfixed); `fill_input` doubling every
character for weeks, invisible because the app returned identical responses for valid and
garbage input (#382).

So state-changing primitives **verify their own write** and return the verdict as an
outcome. A fill reads its value back; a navigation confirms where it landed. This is close
to free — one extra `js()` is **0.8 ms against a 13,100 ms decision**.

> **Verification is nearly free at harness timescales and priceless at agent timescales.**
> The asymmetry only holds because §1's measurement is true.

Two limits, both learned the hard way on a live ATS: read-back **verifies the write, not
the intent** — it proves the DOM holds what you asked for, and cannot know you asked for
the wrong field; and it cannot see a write that legitimately succeeded on the wrong kind of
control (setting `.value` on `<input type=submit>` merely relabels the button). Schema
quality, not verification, prevents that class.

Recovery stops string-matching: typed session states — `attached`, `target_missing`,
`session_stale`, `renderer_unresponsive`, `browser_disconnected` — behind one
`_ensure_live_session()` boundary (#352).

#### Partial work is not success — count it

The fourth failure mode above is the one that silently corrupts *data* rather than breaking
a run. An unbounded fetch fan-out was throttled and returned **163 of ~300** listings; the
call succeeded, nothing was logged, and the number looked plausible. Bounded concurrency
with backoff on 429/5xx recovered it.

Two rules:

- **Any operation over N items reports attempted / succeeded / failed** — never just the
  successes. `fill_form` already does this per field; `fetch_all`, pagination and every
  fan-out owe the same. Silent truncation reads as "covered everything" when it did not.
- **Concurrency is bounded by default and throttling is retried, not dropped.** Unbounded
  fan-out is not faster — it is the same speed with missing data.

#### Completeness is not observable in a single pass

The target itself is non-deterministic, and this is measured rather than assumed. Two
**identical, back-to-back** runs of the same paginated query returned:

| | run A | run B | overlap |
|---|---:|---:|---|
| results | 212 | 174 | 171 shared · **79.5% Jaccard** · 18% count swing |

Offset pagination over a ranked list that reshuffles between requests **structurally**
misses items — no amount of harness correctness fixes it. For comparison, a DOM crawl of the
same query on the same day found 284, and the API's own `total` reported 250. Three methods,
three answers, none of them wrong.

So an operation that claims to have collected "all" of something is making a claim it cannot
support. v2's contract: **report coverage, never completeness** — items found, passes made,
whether the last pass added anything new — and dedupe by stable id across repeated passes so
a caller who needs saturation can run until the yield goes to zero. Cheap here, because a
pass costs seconds: the 15.6 s fan-out can run three times and still beat one 95 s crawl.

And **fail closed**: endpoint binding never widens scope (D10), and neither does session
recovery. v1 re-attaches to `pages[0]`, which after a user's browsing session is *the
user's tab* — observed live during this investigation, silently switching to an unrelated
personal tab after a stale session.

#### (b) The journal: one artifact, three readers

Speed cannot be first-class without traceability, and v1 has none the caller can reach.
`run.py` already computes name, arguments and duration for **every** helper call — then
hands them to `recorder.observe()` (off by default) and `telemetry.capture_cli_event()`
(on by default, and it ships `task=` — the script text — off-machine). The instrumentation
exists; the only enabled consumer is vendor analytics. PR #521, "keep CLI telemetry free of
task data", is still open.

Nothing is persisted locally that would let you reconstruct a session. The daemon log is
eight lines written as `f"{msg}\n"` — **no timestamp, no request id, no PID** — which is
precisely why the silent reattach could not be ordered against any client call.

v2 writes one append-only JSONL per session. Not three artifacts:

```jsonl
{"ts":…,"id":"c7","kind":"invoke","script_sha":"…","script":"…"}
{"ts":…,"id":"c7.1","kind":"call","fn":"goto","args":["https://…"],
 "outcome":{"ok":false,"class":"navigation_failed","landed":"chrome-error://…"},
 "ms":2930,"cdp":4}
{"ts":…,"id":"c7.1","kind":"daemon","event":"stale_session","from":"D058…",
 "to":"CF89…","url":"https://x.com/home"}
```

The shared `id` is the field whose absence made today's forensics guesswork. The same file
serves tracing (`--trace` renders it), forensics (ordered and timestamped), and replay (c).

Output discipline follows the literature: **silent on success**, `--trace` for the span
tree, and **the last N spans dumped automatically on any error** — the failing call in
context without re-running. The actionable number is **CDP round trips**, not just
duration, because that is what design errors look like:

```
fill_input                    174.2ms   61 cdp   ← 3 round trips per character
  └ Input.dispatchKeyEvent ×60 170.1ms   60 cdp
```

Nobody should have to benchmark to discover that a 20-character fill costs 61 round trips.
Attribution separates harness overhead from CDP round trip from *waiting on the page* —
conflating "slow" with "waiting" is how v1's timeouts became false diagnoses.

#### (c) Replay: turn "cannot reproduce" into a file

**The daemon is a perfect record/replay seam** — every byte between client and Chrome
crosses one socket, so nothing in the page or the browser needs instrumenting.

Measured on a real slice (navigate the ATS, extract schema, batch fill, screenshot): 9 CDP
calls, 1.5 KB of requests, 54.4 KB of responses — **of which 49.8 KB is a single
screenshot**. Elide or hash image payloads and a cassette is **~680 bytes per call**; the
160-call field-by-field fill is a **~110 KB fixture you can commit**.

| Mode | Chrome is | Deterministic | Catches |
|---|---|---|---|
| live | real | no | "does the fix work on the real site today" — a smoke test |
| **CDP cassette** | recorded responses | **yes** | harness logic: error handling, session lifecycle, request sequences |
| **DOM fixture** | frozen HTML | **yes** | page-reading logic: schema extraction, label resolution, refs |

Together these would have caught nearly everything found in this investigation: `goto()`
swallowing `errorText` and the silent reattach (cassette), the Abacus label-resolution gap
and the `fill_form` ref misalignment (fixture), `press_key` double-inserting characters
(cassette, asserting the `Input.dispatchKeyEvent` sequence), and the tab-marker 4× regression
(cassette, whose tell was request *order and count*, not output).

**The output is a diff, not a pass/fail** — a golden-file test over the request stream,
which is what catches silent regressions where behaviour still looks correct:

```
$ bh replay sessions/ti8m-apply.jsonl --diff
  ✓ 9/9 calls matched
  ✗ Runtime.evaluate ×1  →  Input.dispatchKeyEvent ×60   (+59 round trips)
```

This is the argument that pays for itself in a repo taking drive-by contributions: **the
corpus is full of "cannot reproduce."** #370 was closed unrooted (*"third party could not
reproduce on main"*), #106 was "fixed" by pinning a dependency the maintainer *could not
reproduce* against, #307 was closed with *"I cannot reproduce the main issue."* A cassette
turns each of those into an attachment that fails on the maintainer's machine.

Limits worth stating: session and target ids change per run, so record and replay both need
normalisation; timestamps and randomness must be pinned; cassettes drift from the live site,
which is fine because a fixture is a frozen artifact, not a mirror; and true timing races
will not reproduce, though request *ordering* catches more than expected.


### D12 — The concurrency unit is a browser context, not a daemon

D1 gives concurrent *tabs*. Chrome also offers `Target.createBrowserContext` —
cookie/storage-isolated contexts **within one connection**, which is what parallel scraping
and multi-profile work actually want.

#311 asked exactly this (`profile=` on `new_tab()` via `browserContextId`) and was declined
because *"it will add a lot of complexity to have one agent juggle two daemons"* — but the
request required no second daemon. The model topped out at one attached page, so there was
nowhere to put the concept.

**v2:** a task owns a context (or a target group) with a lease and idle teardown. This is
also the honest answer to #375, which v1 answers by selling a cloud browser per task, and
to #206, which was closed claiming *"each session and each connection now has a unique
protected daemon"* — **a claim the source does not support**: no refcount, no owner, no
lease, and no `stop_daemon()` at all, only `restart_daemon()`.



### D13 — Event-driven waits, not polling

`wait_for_load`, `wait_for_element` and `wait_for_network_idle` all poll — at 300 ms,
300 ms and 100 ms respectively. Measured cost of the 300 ms interval, on eight runs with
the element appearing at known times:

| element appears | wait returns | overshoot |
|---:|---:|---:|
| 120 ms | 318 ms | 198 ms |
| 250 ms | 306 ms | 56 ms |
| 640 ms | 921 ms | 281 ms |
| 900 ms | 917 ms | 17 ms |

**Median overshoot 168 ms, mean 153 ms, worst case 281 ms** — exactly the ~150 ms expected
from a 300 ms interval, and it is pure waste. `Runtime.addBinding` costs **1.8 ms**.

**v2:** install a binding plus a `MutationObserver` via
`Page.addScriptToEvaluateOnNewDocument`, and let the page *push* the moment a condition
holds. Same mechanism retires several separate problems:

- `wait_for_element` becomes event-driven (−150 ms per wait, and there are many).
- `Page.lifecycleEvent` replaces `document.readyState` polling, which fires before any SPA
  renders — the documented reason `wait_for_element` had to exist at all.
- Snapshot refs stop dying on navigation: the ref machinery is re-installed on every new
  document rather than living in a `window.__bh` that a navigation wipes.
- `Network.*` events already carry a `sessionId`; routing them per session fixes
  `drain_events()` being one global buffer that a background tab can poison.


### D14 — Use more of CDP, not more code

Every method below was probed against Chrome 151 during this investigation and exists. v1
reimplements several of them in Python.

| Instead of | Use | Why |
|---|---|---|
| polling `document.readyState` / `querySelector` | `Runtime.addBinding` + `Page.addScriptToEvaluateOnNewDocument`, `Page.lifecycleEvent` | D13: −150 ms per wait; survives navigation |
| dumping `Accessibility.getFullAXTree` and filtering thousands of nodes in Python | `Accessibility.queryAXTree` | SKILL.md currently tells every agent to re-derive this by hand |
| `getBoundingClientRect()` centre | `DOM.getContentQuads` | correct for inline elements spanning lines, where the rect centre can fall outside the element |
| polling `Target.getTargets` for iframes/popups | `Target.setAutoAttach` | new targets arrive as events |
| re-fetching a response the page already has | `Network.getResponseBody` | the network-tap → API-shortcut pattern, without a second request |
| one global event deque | per-`sessionId` routing | CDP already tags every event; v1 discards that and filters in the consumer |
| `js()` rejecting top-level `await` | `Runtime.evaluate(replMode=True)` | closes a documented leak |

Two that are **not** worth it, measured rather than assumed: `Network.setBlockedURLs` to
block subresources (1.08×, inside the noise) and request batching (~10 ms against a 13 s
decision). And one hazard to encode: **`Fetch.enable` with no handler pauses every request
and wedges the browser** — it hung this investigation's first probe and required
`Fetch.disable` to recover.

### D15 — Batch the *decision*, not just the round trip

D3 makes a single field cheap. This makes the whole form cheap. It is **D0 applied to
forms**: read the form once, decide once, fill once.

Field-by-field, a 20-field form is 20 model round trips ≈ **4.4 minutes of thinking**.
Schema → one fill plan → one batched write is **1 decision, ~13 s**. ~20× on the dominant
cost.

**Measured on four live production application forms, four different ATS platforms** —
real AI-Engineer postings in Zürich. Nothing was submitted and nothing uploaded:

| Company | ATS | Fields | Filled | v1 ms | v1 CDP | v2 ms | v2 CDP | Verified | Identical |
|---|---|---:|---:|---:|---:|---:|---:|---:|:---:|
| ti&m | Abacus/Umantis | 20 | 5 | 248 | 155 | **7** | **1** | 5/5 | ✓ |
| pro-informatik | custom PHP | 10 | 5 | 238 | 142 | **7** | **1** | 5/5 | ✓ |
| Corealis | Personio | 7 | 2 | 248 | 107 | **8** | **1** | 2/2 | ✓ |
| Luware | FactorialHR | 33 | 7 | 231 | 144 | **6** | **1** | 7/7 | ✓ |
| **total** | | **70** | **19** | **965** | **548** | **28** | **4** | **19/19** | ✓ |

**34× faster, 137× fewer CDP round trips, byte-identical form state on every one.** v1 has
no verification at all; v2 verified all 19 writes. The milliseconds remain the uninteresting
half: 19 fields filled one at a time is 19 model decisions (~4 min) against 4 (~52 s).

A smaller run on httpbin (10 mixed fields) gave the same shape — schema in **175 tokens**
versus ~3,180 for a screenshot of the same form, batch fill of 8 fields in 5 ms.

#### Second live run: the implementation, against six forms it had never seen

The table above was measured on a prototype. The built version was re-run end-to-end
(2026-08-05) against a fresh search — jobs.ch, "AI Engineer" in Zürich, 469 hits — with
**both harnesses driving one shared scratch-profile Chrome**, so cookie state and network
were identical. Nothing submitted, nothing uploaded, no accounts.

| Form | ATS | flds | v1 CDP | v1 ms | v2 CDP | v2 ms | CDP × | ms × |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Pro Informatik | custom PHP | 5 | 158 | 259 | **2** | 16.3 | 79× | 15.9× |
| Luware | FactorialHR | 6 | 507 | 763 | **2** | 17.3 | **254×** | 44.1× |
| ZHAW | Prospective/Refline | 12 | 405 | 658 | **2** | 26.4 | 202× | 24.9× |
| ZHdK | Refline | 12 | 404 | 630 | **2** | 25.8 | 202× | 24.4× |
| Swisslinx | custom | 5 | 424 | 858 | **2** | 20.0 | 212× | 42.9× |
| AutoForm | jobs.ch native | 5 | 423 | 1170 | **2** | 47.5 | 212× | 24.6× |
| **total** | | **45** | **2,321** | **4,337** | **12** | **153** | **193×** | **28.3×** |

Both filled 45/45. The honest caveats, stated because they change how the numbers read:
v1 was **handed v2's selectors** — the primitive it does not have — so this measures
mechanics, not the discovery work v1 would additionally owe; and v2's default settle-recheck
adds a fixed 150 ms per form, making the true end-to-end **1,001 ms / 4.3×** rather than
28×. The structural result is the durable one:

> **v1's round trips scale with *characters*; v2's are constant per form.** 2,321 calls for
> 691 characters, 95% of them `Input.dispatchKeyEvent`. Luware needed **507 calls for six
> fields** purely because its text was long. v2 is 2 (schema + write), regardless of field
> count or text length.

#### What six unseen forms broke that four fixtures could not

Both bugs were **false success**, the mode D11 calls the expensive one, and neither is
reachable from a synthetic fixture:

1. **A widget with no `value` property at all.** jobs.ch's phone-country control is a
   `DIV[role=combobox]`. The writer treated it as a text input and invoked
   `HTMLInputElement`'s value setter on a DIV → `TypeError: Illegal invocation`. The
   damning part: `form_schema` had **already** flagged it `needs_interaction` and the
   writer did not read its own schema's finding. Hence `Class.NEEDS_INTERACTION` — kept
   distinct from `no_option_match` because the recovery differs (click the popup and pick
   vs. supply a different label), and a shared class would collapse two different repairs.

2. **A decoy that satisfies the success check.** Select2 leaves a **1×1 pixel,
   `clip: rect(0,0,0,0)` "focusser" input** where the real control was, and keeps the real
   250-option `<select>` clipped *in place*. The schema picked the decoy; writing `"CH"` to
   it **stuck, read back byte-identical, and verified** — while the element that actually
   submits stayed empty. This is the worst possible outcome: not a failure, a *confirmed*
   success that is false. Two rules follow. **A control a human cannot see is not a field**
   (clipped or ≤2 px ⇒ excluded). **A hidden `<select>` is still the form's data** ⇒
   surfaced as `hidden_control`, because it is the element the browser will submit.

The general principle both share, and the reason a fixture suite cannot replace a live run:
*verification is only as good as its notion of identity.* Read-back proves a value landed
somewhere; it cannot prove it landed on the control that matters. That is now the fourth
independent instance of **read-back verifies the write, not the intent** — after the submit
button, the ref misalignment, and the +34 prefix.

#### Attrition is the real cost of an apply pipeline, not fill speed

Of 21 postings, **six were fillable**. The other fifteen (measured, not estimated):

| Cause | n | Note |
|---|---:|---|
| Account wall / non-form landing | 5 | SuccessFactors, gohiring, Outlook safelink, solique, aerztekasse |
| Redirected away to another portal | 3 | ContactRH → careers.snb.ch |
| **Bot protection** | 2 | SmartRecruiters behind DataDome: body `innerText.length === 0`, 10 DOM nodes, all real content inside a `geo.captcha-delivery.com` iframe |
| Duplicate flow | 4 | same jobs.ch native form |

Two consequences for the design. First, **`form_schema`'s verdict is load-bearing at
pipeline scale**: it is what turns "0 fields on a page that renders fine" into a typed
`not_a_form` instead of a silent no-op — and it correctly refused all four non-forms here.
Second, **bot protection is an endpoint property, not a form property**: the DataDome page
is indistinguishable from a slow SPA by any DOM measure, which is the strongest argument
for the managed-IP cloud browsers in D12 rather than for more retry logic.

#### What a page can actually observe (and the premise worth correcting)

*Prompted by a direct question: does v2 even use CDP, and is v1's different shape a stealth
decision? Measured rather than argued.*

**Both are CDP.** v1 connects via `cdp-use`, v2 via `websockets`, to the same DevTools
endpoint, with the same `Target.attachToTarget(flatten)` model. There is no protocol-level
difference to trade stealth against. What differs is *input synthesis* (above) and, until
this run, one artefact v2 was leaving on the page.

Probed from inside a page, with the page reporting over plain HTTP so the measurement
itself involves no CDP:

| Signal | Cause | v1 | v2 (before) | v2 (now) |
|---|---|---|---|---|
| `navigator.webdriver === true` | **the `--remote-debugging-port` flag alone** — proven: true with the flag and *nobody attached*, false without it | same | same | same |
| `window.__bh` visible on `window` | v2's injected page runtime (D13/item 18) | **absent** | **present** | **absent** |
| `console.debug` serialise timing | `Runtime.enable` (2.3 ms vs 0.3 ms for the same payload) | same | same | same |
| `isTrusted: false` on input events | one-shot value writes | n/a — v1 types | default | default; `mode` opts out |

Three conclusions, in order of how much they matter.

**1. `navigator.webdriver` is set by the flag, not by the attach, and not by either
harness.** Chrome flips it the moment remote debugging is enabled on the command line —
measured with nobody connected at all. No harness-level change can hide it, and v1 and v2
are equally exposed. This is worth knowing precisely because it is the signal people assume
their automation library controls. (Untested and worth testing: whether enabling debugging
at runtime through `chrome://inspect` — v1's documented local flow — differs from the
launch flag.)

**2. `window.__bh` was a self-inflicted wound, and is fixed.** v2 injected its ref registry
onto the page's `window`, where any script could enumerate it. v1 never did this, because
it has no persistent injected runtime — so on this axis the intuition that "v1 is doing
something different" was correct, and v2 was the worse citizen. The fix is a CDP **isolated
world**: same DOM, separate global object, recreated on every navigation for free via
`Page.addScriptToEvaluateOnNewDocument(worldName=…)`. Page script now sees nothing.
`js()` deliberately stays in the main world — it is the user's escape hatch, and code that
reaches for page globals must land where those globals live. This is D14 in its purest
form: the alternative was obfuscating a global name, which only raises the cost of finding
it rather than removing it.

**3. `Runtime.enable` is a measurable side channel** — a logged object serialises ~8×
slower with the domain enabled. Both harnesses enable it by default. v2 can at least *make
it optional*, since `DEFAULT_DOMAINS` is a registry parameter and the isolated world no
longer needs main-world `Runtime` for its own machinery; that is left as a documented knob
rather than a default change, because turning it off costs `js()` and console capture.

**Scope, stated once.** The defensible goal is that the harness should not *announce*
itself for no benefit — a stray global and an unnecessary domain are bugs by that standard,
and both are now addressed. The undefensible goal is defeating anti-bot systems: it is an
arms race, `navigator.webdriver` alone makes it unwinnable from inside the page, and the
DataDome wall in the six-form run is the evidence — a fresh profile with a clean DOM was
CAPTCHA'd on **IP and TLS reputation**, which no amount of in-page tidiness touches. That
is what D12's managed cloud browsers are for, and it is also where automating a site that
forbids it stops being an engineering question.

#### Why v1 is slow, and what its slowness was buying

*The 193× invites a bad conclusion — that v1 was simply doing it wrong. It was not, and
this table is the counterweight D0 demands. v2 is fast because it does **less**, and the
"less" is not free.*

v1 types **character by character** via `Input.dispatchKeyEvent`. That is 3N round trips
per field and it is why Luware cost 507 calls. Measured on a page instrumented to count
what each write actually triggers:

| Write mode | round trips | `isTrusted` | keydowns | typeahead opened | dropdown |
|---|---:|---|---:|---:|---|
| v2 `value` (default, one-shot) | 1 | **false** | 0 | **0** | *empty* |
| v2 `insert` (`Input.insertText`) | 2 | true | 0 | **0** | *empty* |
| v2 `type` / **v1's model** (`dispatchKeyEvent`) | ~3N | true | 18 | **3** | `"suggestions for zur"` |

Three things v1's cost buys, none of which a one-shot write provides:

1. **Trusted events.** v2's default fires `new InputEvent(...)` from page script, which is
   `isTrusted: false`. Any page that checks it — some payment and anti-fraud flows do —
   rejects the value.
2. **Per-keystroke side effects.** Typeahead pickers (location, school, skills), masks that
   format incrementally, character counters, validate-as-you-type. The measurement is
   unambiguous: the one-shot write left the typeahead's dropdown **empty**.
3. **A plausible interaction shape.** 45 fields written in 16 ms with zero keydowns and no
   pointer movement is itself a bot signal.

**The finding that changed the design:** `insert` does **not** subsume `type`.
`Input.insertText` produces *trusted* events yet still zero keydowns, so it fixes (1) and
does nothing for (2). Two tiers were therefore not enough, and `set_value` now has three —
`value` / `insert` / `type` — with `type` reproducing v1's model exactly, for the fields
that need it.

So the honest statement of the trade is not "v2 is 193× faster." It is:

> **v1 pays the faithful-simulation cost on every field; v2 pays it only where a field
> demands it.** The default is a *bet* that most fields are ordinary inputs. That bet held
> on 45/45 fields across six production ATS forms — but it is a bet, and the escape hatch
> is part of the design, not an afterthought.

The same asymmetry runs through D0: batching converts *walk-the-loop* decisions into
*debug-the-batch* decisions. Both bugs above are exactly that — a batch that hit one
unusual widget, and had to be debugged as a batch. What makes the trade pay is not that
batching never fails; it is that **the failure is typed, per-field, and does not stop the
other 44**.

**Note what carried across four unrelated ATSs**: all four expose first name, last name and
email under different labels and languages (`First`/`Last`, `Vorname`/`Nachname`,
`first_name`, `customeraddressshoppervorname`). Four recipes covered four employers — and
Personio and FactorialHR alone cover thousands of European companies. That is D6's
per-platform cache, confirmed against production rather than assumed.

**The schema is the design.** Not "the input elements" — what makes a field *fillable*:
`type`; `required`; **the valid `options`** for selects and radio groups, so the model picks
from real values instead of guessing; `pattern` / `maxlength`; current value; `autocomplete`;
and above all a **label**.

**Label resolution is the hard part, and the standard chain is not enough.** On the ti&m
form the `Vorname` input has no `id`, no `aria-label`, no `placeholder`, no `label[for]`
and no ancestor `<label>` — the visible "Vorname *" is simply the **previous sibling of the
input's row**. The documented chain (`aria-label` → `aria-labelledby` → `label[for]` →
ancestor label → placeholder → legend) resolves *nothing*, and the schema degrades to raw
`name` attributes:

```
before proximity fallback          after
"customeraddressshopperanrede"  →  "Anrede *"
"LAA__USERFIELD22"              →  "Frühestes Startdatum"
"LAA__USERFIELD11"              →  "Personalvermittlung: Firma"
```

`LAA__USERFIELD22` is unguessable; "Frühestes Startdatum" is trivial. So the chain gets a
final **proximity fallback**: walk up a few levels and take the nearest previous sibling
whose text is short and contains no form controls. A synthetic form never surfaces this —
httpbin uses proper `<label for>` and passed cleanly. Real ATS markup is where the
requirement lives.

That last field carries disproportionate weight. `autocomplete` is a *standardised
vocabulary* — `given-name`, `email`, `tel`, `address-line1`, `postal-code`. A form that
uses it correctly can be filled from a precomputed answer bank with **zero** model
decisions. Combined with D6, the first encounter with an ATS costs one decision, the field
map is written back as a skill, and every later application to that platform costs none.

**Verification is what makes batching safe, and this is not hypothetical.** The prototype's
first run misaligned refs by one — because schema extraction and fill used slightly
different element filters — and the form still reported `form_valid: true` and
`submit_enabled: true`. It looked perfect. Without read-back, an agent would have submitted
an application with the phone number in the delivery-time field and reported success. That
is #382 exactly: `fill_input` doubled every character for weeks, invisible because the app
returned identical responses for valid and garbage input. So: **one extraction routine
shared by schema and fill** (two filters is how refs drift), and every batch returns a
per-field `ok` plus `got`/`want` on mismatch.

**Buttons are never fillable, and verification will not save you here.** On the ti&m run a
planner matched the substring `ort` inside `jobportal_taca` and `jobportal_application_submit`
and wrote a city name into a checkbox *and into the submit button's value*. Read-back caught
the checkbox. It did **not** catch the button — setting `.value` on `<input type=submit>`
genuinely succeeds; it just relabels the control. Two rules follow, and only the first is
about verification:

- Exclude `submit`, `button`, `reset`, `image` and `file` from the fillable set at
  *extraction* time. A control that cannot hold user data must never reach a fill plan.
- Match field intent on **word boundaries, never substrings**. `ort` ⊂ `jobportal_taca` is
  the kind of collision that reads as a typo and behaves as a data-corruption bug.

The general lesson is that **read-back verifies the write, not the intent.** It proves the
DOM holds what you asked for; it cannot know you asked for the wrong field. Schema quality —
labels, types, and excluding non-fields — is what prevents the class of error verification
is blind to.

#### The reliability bottleneck is *page identity*, not filling

Across five attempted postings, **filling never failed** — 19/19 fields verified on the four
that worked. **Three of five apply links were dead**, and the harness reported success on
all three:

| Target | Actually landed on | What the harness saw |
|---|---|---|
| SNB / ContactRH | `careers.snb.ch/errorpage/?errortype=404` | **8 fields** → classified usable |
| ZHAW / Refline | *"Bewerbung konnte nicht gefunden werden"* — posting withdrawn | 0 fields |
| Luware / FactorialHR | the job **list**, not the posting | **6 fields** → classified usable |

The 8 "fields" on the 404 page were a cookie-consent banner and a search box; the 6 on the
job list were location filters and cookie toggles. **Field count is not evidence that you
are on an application form.** Two dead pages passed a "does this have inputs" check — D11's
*undefined success* in its most expensive form, because the very next step writes a person's
name, email and phone number into whatever happens to be there.

So `form_schema()` owes a **form-identity verdict**, not just extraction:

- **Positive signals** — a name or email field present; a submit control whose text matches
  apply / bewerben / senden.
- **Negative signals** — page text matching *not found* / *nicht gefunden*; a URL containing
  `errorpage` or `404`; landing somewhere other than the posting that was requested (compare
  `requested` vs `landed`, which D11's outcome contract already carries).
- **Classify out the furniture** — cookie-consent checkboxes, search boxes and result filters
  appear in nearly every schema, are never part of an application, and inflate exactly the
  count that the proceed/abort heuristic reads.

The rule: **an operation that writes personal data must refuse to run until the page has
affirmatively identified itself.** A redirect that silently substitutes a list page for a
posting is a failure, not a success.

#### Dropdowns are their own problem, and verification is blind to all of it

Selects looked handled and were not. Across the same four forms:

| Form | `<select>` | What was actually there |
|---|---:|---|
| ti&m | 3 | 2 of 3 have an **empty placeholder** as `options[0]` |
| pro-informatik | 3 | **all three** are placeholders — `─ Nationalität ─`, `─ Jobs ─` |
| Corealis | 1 | `gender` → `"Please select"` first; never matched at all |
| Luware | 1 | `phone_prefix` → **`+34` (Spain) selected from 249 options** on a Swiss application |

Three distinct defects, none of which read-back can see:

1. **Placeholder-first.** Seven of eight selects lead with a non-answer. Choosing
   `options[0]` frequently means *selecting nothing* while reporting success. Filtering
   empty values only works by luck — placeholders are often `"0"`, `"none"`, `"-1"`, or a
   real value labelled *Please select*. The schema must mark a placeholder, not hope it is
   falsy.
2. **Blind first-option.** A stand-in planner picking `options[0]` produced Spain's dialling
   code for a Zürich application. The schema exists precisely so the *model* chooses from
   real values; any code path that picks for it is a bug generator.
3. **Long option lists.** 253 countries, 251 nationalities, 249 prefixes. Inlining them is
   unaffordable in tokens; truncating them (this prototype cut to 10) makes the correct
   answer **unreachable** — Switzerland is simply not in the list the model sees. Neither
   extreme is acceptable, so long lists need a count plus in-page resolution: the plan
   carries a *label* (`"Switzerland"`, `"+41"`), and `fill_form` matches it against the full
   option list inside the page, returning `no_option_match` when it cannot.

**All three passed verification**, because `got == want == "+34"`. This is the third
independent instance of *read-back verifies the write, not the intent* — after the submit
button and the ref misalignment — and the clearest: the value was written perfectly, to the
right field, and was wrong.

Beyond native `<select>`, **custom combobox widgets are invisible to extraction entirely**
(`role=combobox` / `aria-haspopup=listbox` with no `<select>` behind them). Corealis had two.
On SmartRecruiters, Workday and Ashby whole forms are built this way, so `form_schema()`
returns nothing and `fill_form()` fills nothing while both report success. The schema must
enumerate ARIA widgets alongside native controls and mark them `interactive` — they need a
click-and-select interaction, not a value assignment.

Where naive one-shot batching breaks, and what the design owes each case:

- **Dependent fields** — country changes state options; a plan choice reveals new fields.
  Batching is therefore *staged*: fill what exists, re-read schema, fill again. Converges in
  2–3 passes, not N.
- **Typeahead widgets** — location and school pickers need real keystrokes plus a dropdown
  selection. Detect via `role=combobox` / `aria-autocomplete`, mark `interactive`, exclude
  from the batch.
- **Blur-only validators** — the writer fires focus → input → change → **blur** per field;
  many validators listen only for the last one.
- **Framework state** — native setter plus real events covers React/Vue. Ember/Glimmer
  remains the accepted limit (§6, #148).
- **Anti-bot timing** — 8 fields in 5 ms is a signal. Pacing is a per-site decision, never
  a default.

---

## 4. Helper surface

**Admission test:** does it collapse *steps*, or merely wrap a CDP call? `goto()` earns its
place (3 round trips → 1). `snapshot()` earns it (replaces a 194 ms screenshot + an
estimated coordinate with 8.6 ms and an exact one). A `cdp()` wrapper does not.

Core, in: `goto` · `snapshot` / `click_ref` / `fill_ref` · `form_schema` / `fill_form` ·
`page_text` · `js` · `cdp` · `click_at_xy` · `press_key` · `set_value` · `scroll` ·
`wait_for_*` · tab ops · `capture_screenshot` · `upload_file` · `http_get` ·
`skills.*` (D6 — four calls, no content).

Proposed additions, each justified by a measurement:

| Helper | Why |
|---|---|
| `form_schema(scope=None)` | D15. Whole form as **175 tokens** vs ~3,180 for a screenshot: label, type, required, valid `options`, constraints, `autocomplete`. Turns an N-decision form into a 1-decision form. Returns a **form-identity verdict** — 3 of 5 live apply links were dead and two passed a naive has-inputs check. Label resolution needs a **proximity fallback**; a real ATS resolved *nothing* through the standard chain. Excludes buttons, file inputs, and cookie/search furniture at extraction; marks **placeholder options** (7 of 8 selects lead with a non-answer) and enumerates **ARIA combobox widgets**, which have no `<select>` and are otherwise invisible. Excludes **clipped/≤2 px decoys** — Select2's 1×1 `clip-rect(0,0,0,0)` focusser accepted a write, verified, and submitted nothing — while surfacing the **hidden real `<select>`** behind them as `hidden_control`, since that is the element the browser submits. Must share its extraction routine with `fill_form` — two filters is how refs drift. |
| `fill_form(plan)` | D15. **2 CDP round trips vs 2,321** across six live ATS platforms (45/45 verified; v1's calls scale with *characters*, v2's are constant per form), each field with focus→input→change→blur and per-field `ok` + `got`/`want`. Accepts a **label** for long option lists and resolves it in-page (`no_option_match` when it cannot) — truncating 249 options made the right answer unreachable and silently selected Spain. Refuses a widget with no `value` property as `needs_interaction` rather than throwing `Illegal invocation` on it. Verifies after a **settle**, because a normalising control rewrites the value it accepted; verifies the *write*, not the *intent*, and never runs until `form_schema` has identified the page. |
| `click_ref(n)` / `click_at_xy` | D11. Returns a **change delta** — url, new tab, DOM node count, dialog opened + its text. A click's effect is not a readable value, so "no exception" says nothing. Cost ~1 ms; not having it cost a minute and a wrong diagnosis on jobs.ch. |
| `fetch_all(urls, concurrency)` | D0's primary instrument. In-page, session-authenticated: **6.1×** on pagination (95.2 s → 15.6 s) and **12.5×** fanning out over 7 candidate pages (35 CDP round trips → 1, 7 decisions → 1). Hand-written twice in one session and wrong twice — unbounded fan-out silently returned 163 of ~300 results with no error. Bounds concurrency, retries throttling, and returns **attempted / succeeded / failed** rather than just results. Silent data loss is the strongest argument for code over prose. |
| `set_value(target, text, mode=)` | D3. **Three tiers, because two were measurably not enough**: `value` (1 round trip, `isTrusted: false`), `insert` (trusted, still no keydowns), `type` (~3N, trusted, per-keystroke — v1's model, kept for typeaheads and incremental masks, which the first two leave *untouched*). The single-field primitive `fill_form` is built on. |
| `use_tab(target_id)` | D1. The client-side half of session pinning. |

Note the shape these share: **each collapses a round trip that costs a model decision**, and
each returns enough to verify itself. `form_schema` + `fill_form` are the clearest case —
they exist precisely because the alternative is N decisions, not because filling a field is
hard.

Deliberately **prose, not code** — the right answer is site-dependent, so a helper would
guess wrong:
- *verifying an action landed* (URL change vs DOM mutation vs network idle)
- *when keystroke-level typing is actually required* (D15's `interactive` flag marks the
  fields; choosing the strategy stays a judgement call)

---

---

## 5. How v2 gets built: the smallest diff wins

The clearest pattern in 309 resolved PRs: **a maintainer reads the contributor's diagnosis,
discards the implementation, and lands a version 5×–60× smaller.** Every supersession:

| Problem | Rejected | Landed |
|---|---:|---:|
| Windows IPC | +587 / +545 / +542 / +384 / +256 | **#225: +136/−54** |
| Chrome 147 `/json/*` 404 | +764 / +392 / +27 | **#265: +12/−2** |
| IPC socket TOCTOU | +252 / +163 / +31 | **#309: +4/−2** |
| doctor stale connections | +66/−29 | **#254: +25/−11** |

Verbatim: *"This PR is far too verbose, the fix only needs a few lines."* And the repo's own
`AGENTS.md`: *"Consider what is really needed. Prefer the smallest diff that fixes the
bug."* Every refactor that actually landed was **subtractive** — `keep browser primitives
AX-only` (−373), `Simplify LLM browser interface` (−151), `Trim skill docs`.

**Implication for the fork:** the diagnoses in this document are the asset. Resist shipping
them as large subsystems. A decision here that cannot be expressed in tens of lines should
be re-examined before it is built.

## 6. Non-goals

| Not building | Evidence |
|---|---|
| Request batching | measured ~10 ms saving vs 13 s decisions |
| Subresource blocking | measured 1.08× |
| WebDriver BiDi / cross-engine | #564; cost is a second adapter across every helper |
| Implicit focus management | measured unnecessary (D2); 5 closed + 4 open PRs |
| Screenshot-based element finding | 194 ms + image tokens vs 8.6 ms + exact coordinates |
| Retry/robustness wrappers, page-object DSL | agent-written `js()` costs microseconds |
| **A transport abstraction layer** | 13 rejected Windows PRs (+178…+587) lost to a `platform.system()` branch (#225, +136). `BU_DAEMON_TRANSPORT=tcp` (#122) abandoned after weeks. |
| **Env-var knobs for per-call behavior** | every one rejected: `BH_NO_ACTIVATE` (#469), `BH_KEEP_TABS` (#354), `BH_NO_AUTO_DISMISS` (#316), `BU_CDP_HTTP` (#324). Surviving shape is an explicit parameter. *(This indicts `BH_IPC_TIMEOUT`, added during this investigation — make it a per-call argument.)* |
| **Domain skills in-tree** | 30 closed, 91 open-unreviewed, 60 merged **then demoted** to `agent-workspace/` and gated `BH_DOMAIN_SKILLS=1`, **off by default**. Sites rot faster than review: #159 closed with *"Spirit just went out of business today."* |
| **Plugin / marketplace packaging** | killed 3× (#443→#444, #447, #453). Correct model per #453: CLI dependency, with a hosted MCP as the install-free answer. |
| **Enterprise/ops surface** (smoke CLIs, audits, watchdogs) | #117 (+2,682) and #378 (+690), both silently closed. |
| **Re-litigating the heredoc CLI** | `-c` merged (#188) → docs PR closed (#215) → fully reverted (#343). Settled: stdin/heredoc. |
| **Humanized input / fingerprint evasion** | #248, #317. Declined as open-core boundary; the proposal's own rebuttal stands — LLMs get the Bezier/Fitts math wrong, and per-session variation is itself a fingerprint. |

### Accepted limits (document, don't promise to fix)

- **Framework-owned state is unreachable by CDP input** (#148). Ember/Glimmer keeps tracked
  state off the DOM with no registry and no `__ember_meta__`; the submit handler no-ops with
  **zero network request**, so there is nothing to intercept or replay. Sixteen approaches
  failed over ~2 hours at 100% repro. No input primitive fixes this. v2 states it as a
  boundary and ships a native-setter escape hatch, rather than leaving it open as if a
  helper will land.
- **Snap-confined Chromium cannot expose CDP at all** (#191, #328). Detect and refuse with
  an instruction; do not attempt to work around confinement.

---

## 7. What we keep from v1

Scar tissue that looks like cruft and is not — each was paid for in a real failure:

- **Chrome 144+** per-connection "Allow remote debugging?" popup: one held connection = one
  click. Retrying in a loop re-prompts forever.
- **Chrome 147+** disables `/json/*` discovery on the default profile — fall back to the ws
  path in `DevToolsActivePort`. (Confirmed today: `/json/version` returns nothing on this
  machine's default profile.)
- **Stale `DevToolsActivePort`** from a closed browser must not count as a live instance.
- Per-profile discovery paths across macOS/Linux/Windows and Chrome/Edge/Brave/Chromium.
- **Per-character `Input.dispatchKeyEvent` typing.** The most expensive thing v1 does
  (3N round trips per field; 95% of its 2,321 calls in the six-form run) and the easiest to
  mistake for waste. Measured, it is the *only* write mode that fires keydowns, so it is
  the only one a typeahead or an incremental mask can see — `Input.insertText` produces
  trusted events and still zero keydowns. Kept verbatim as `set_value(mode="type")`.
  **Demoted from default to opt-in, not deleted**: the mistake would have been assuming a
  cost is unnecessary because its benefit is invisible on the forms you happened to test.

Do not start from a blank file. The core worth carrying is ~1,500 lines
(`helpers` + `daemon` + `_ipc` + `run` + `paths`); the other ~3,000 (`admin`, `auth`,
`telemetry`, `recorder`, `video`) should be optional layers or dropped. `admin.py` alone is
1,056 lines and never touches a page.

---

## 8. Leaky abstractions to close

| Leak | Effect |
|---|---|
| 🐴 title marker | harness mutates `document.title`; **every scraped title carries the emoji** — verified: `page_info()` returns `'🐴 1391 Software Engineering jobs'` while `og:title` is clean |
| modifier keys picked from `sys.platform` | client OS, not browser OS — sends Cmd to a Linux cloud browser; select-all silently fails |
| `new_tab()` may reuse the current tab | name lies; returns a targetId either way |
| `goto_url()` returns before load | every caller must pair with `wait_for_load()`; failure is silent (reads the old DOM) |
| `js()` rejects top-level `await`; returns `None` for non-serializable values | `Runtime.evaluate` semantics showing through |
| `drain_events()` is one global buffer | not session-scoped; consumers filter by hand |
| `http_get()` switches transport on `BROWSER_USE_API_KEY` | different IP and failure modes, same signature |

---

## 9. Open questions

1. ~~Process model — one daemon multiplexing N clients, or one per client?~~
   **Resolved by measurement (D7): one daemon per browser endpoint, multiplexing N clients.**
   Consent is per-connection, so per-client daemons put a serialised modal in front of every
   subagent. The daemon is a property of the *browser*, not the client — so process isolation
   follows browser isolation, which is also how this composes with #454's explicit
   browser-selection model rather than competing with it.
2. How does a pinned client recover when its tab is closed by the user?
3. Do we take #454's explicit browser-selection model (`browser_new` / `browser(id)`) for
   *browser* isolation on top of D1's *tab* isolation? They are orthogonal and compose.
4. Skills: who hosts the first `community` index, and does it need signing on day one or is
   digest-verification plus the no-authority trust tier (D6) sufficient to start?
5. Gating (D0): if v2 needs human approval for hard-to-reverse actions, what is the
   declarable action layer above the REPL? Arbitrary code cannot be approved, and
   "containerize it" is v1's answer, not a design.

*Resolved since drafting:* registry format and trust model for skills — see D6 and
`docs/skills-plugin-system.md`.

---

## 10. Reproducing the measurements

Scripts used are in the session scratchpad: `bench.py` (primitives), `paginate.py`
(3-mode task comparison), `shotbench.py` (screenshot variants). All run against a real
attached Chrome, never headless. Re-run before trusting any number here after a Chrome
update — half of these constants are Chrome's, not ours.
