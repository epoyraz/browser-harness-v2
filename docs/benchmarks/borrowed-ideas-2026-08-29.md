# Helpers worth taking from v1, Stagehand and agent-browser — read against today's data

2026-08-29, evening. Companion to `stagehand-review-2026-08-29.md` and
`agent-browser-review-2026-08-29.md`, which rank each codebase on its own terms. This note
does two other things: it re-ranks the borrowable ideas by what **today's measurements**
(`ats-map-2026-08-29.md`, `experiments-2026-08-29.md`) say actually costs decisions,
seconds or reliability; and it answers the design question v1 raises by its layout —
`src/browser_harness/helpers.py` is code, `interaction-skills/*.md` is prose — why, and
what v2 should keep of that.

## 1. Why v1 splits helpers (code) from interaction skills (markdown)

`helpers.py` is 538 lines of **mechanism**: `cdp`, `js`, `click_at_xy`, `press_key`,
`new_tab`, `switch_tab`, `wait_for_element`, `upload_file`, `http_get`. Each is a verb
over CDP that is true on every page. `interaction-skills/` is 18 files, 433 lines of
**procedure**: how to survive a `beforeunload` dialog, which tab order the user sees on
macOS versus Linux, when to stub `alert()` and what that costs in detectability. Read the
two substantive ones (`dialogs.md`, `tabs.md`) and the design is obvious:

| | helpers (code) | interaction skills (markdown) |
| --- | --- | --- |
| what it holds | verbs that are always true | situations, trade-offs, platform forks |
| who executes it | the harness | the model, after reading and adapting |
| when it is loaded | always (pre-imported into the script namespace) | on demand — `SKILL.md`: *"if you get stuck on a browser mechanic, check interaction-skills"* |
| how it changes | a PR with tests | a PR with prose; no tests, no release coupling |
| cost per use | one CDP round trip | **one model decision** (read, adapt, run) |

The advantages of the markdown side are real and v1 got them for free:

1. **Progressive disclosure.** The model reads `dialogs.md` only when a dialog freezes the
   page. Code cannot be lazily loaded into a model's attention; text can. This is the same
   argument agent-browser makes with its 52-line stub plus 4,537 lines served on demand.
2. **Trade-offs are prose.** `dialogs.md` says the CDP path is undetectable and the JS
   stub is detectable but handles sequences; `tabs.md` says CDP cannot see the visible tab
   strip and hands you AppleScript or `xdotool`. A helper would have to pick one; prose
   lets the model pick per situation. Knowledge with a *"depends"* in it does not compile.
3. **Cheap to author.** A contributor who can describe a site's quirk can ship it without
   touching the 2,204 lines of control code. That is why v1 could accept 97 domain-skill
   directories and queue 109 more (`skills-plugin-system.md`).
4. **The code stays small.** v1's whole thesis is a 2.2k-line substrate. Everything with
   a shelf life went to markdown so the code would not accrete it.

And the costs, which are just as visible in the same directory:

1. **It rots silently.** 8 of the 18 files are one-sentence stubs — `shadow-dom.md` is
   *"Focus on recursive `shadowRoot` traversal, and note when coordinate clicking is
   simpler"*; `uploads.md` is a title. Nothing fails when a skill is empty, so the
   catalogue reads as complete and is not. Code that thin would not import.
2. **Every use is a decision.** Today I hit exactly the `shadow-dom.md` situation on
   SmartRecruiters and did what the stub says: wrote a recursive `shadowRoot` walker by
   hand (`ats-map/inspect_js.py`). That is the price of knowledge-as-prose: it is paid
   again by every agent, every time, and it is where the "133 raw `js()` calls across
   three tasks" in v2's README come from.
3. **It cannot be measured.** No telemetry says whether `dialogs.md` helped. v2's
   `skills-plugin-system.md` ranks skills by *local success rate*; v1 has nothing to feed
   that with.
4. **The good ones want to be code.** `dialogs.md`'s CDP recipe — detect via
   `page_info()`, `Page.handleJavaScriptDialog`, read the message from events — is exactly
   what v2 turned into a helper consequence (`click_ref` reports the dialog; `press_key`
   refuses a submitting Enter). Once a procedure has no *"depends"* left in it, prose is
   the wrong container.

**The rule this gives v2.** Sort knowledge by its half-life and its branching, not by
convenience:

- *Mechanical and stable* (dialogs, shadow roots, hidden tabs, opener trees, network
  quiet) → **code**, and specifically a helper whose failure carries the recovery line.
  v2's typed `Outcome.recovery` is the interaction skill delivered at the moment it is
  needed, at zero reads. Today's `blank_while_hidden` + *"call activate_tab() and read
  again"* is the pattern: the skill is the error message.
- *Site- or vendor-specific and volatile* (which button is "apply" on Prospective, that
  `atsconnector.prospective.ch/<umantis|successfactors>/` names the backend, that Umantis'
  password field is optional, that Workday always walls) → **data**: markdown with
  frontmatter match rules, versioned, digest-pinned, ranked by measured success. v2 built
  this (`harness/skills.py`: `match`, `search`, `load`, `sync`) and it holds **zero
  skills**. The ATS map produced its first corpus today: 57 vendors, per-vendor apply
  mode, connector paths, the four vendors that paint only when visible.
- *Trade-offs with a genuine "depends"* (stealth vs stub, visible tab order per OS) →
  prose, on demand, and short.

What v2 should **not** copy is the one-directory-per-hostname catalogue with no index:
the design note already says why, and v1's stub files are the evidence.

## 2. Helpers worth taking, re-ranked by today's evidence

Ranking = (decisions or seconds or failures it would have removed *today*) ÷ effort.
Licence positions from the two reviews stand: borrow the design, write our own.

| # | idea | source | what today measured | effort |
| --- | --- | --- | --- | --- |
| 1 | **Resolved-action record** — `{selector, method, arguments, description}` that outlives the document, with `selfHeal` and a hit threshold | Stagehand `observe → act(Action)` | The ATS map located "Jetzt bewerben" ~1,000 times by a hand-written regex over button text. Per vendor that button is stable: one resolved action per ATS (57 of them) would have made every hop after the first a zero-decision replay | medium |
| 2 | **Content boundary with a CSPRNG nonce**, origin-tagged | agent-browser `output.rs` | 500 employer pages of text went through my context today with no fence. Only hole on the list | low |
| 3 | **No-browser `read`** (HTTP, markdown-first, `llms.txt`) | agent-browser `read.rs`, v1 `http_get` | Stages 1–2 of the ATS map (1,500 SSR pages) ran in `requests` *outside* the harness because `fetch_all` needs a live page; v2's SKILL says "no browser for public pages" and offers nothing for it | low |
| 4 | **Daemon restart / idle timeout** — `bh --reload`, and a fail-safe idle exit | v1 `--reload`, agent-browser `IDLE_TIMEOUT` | The corpus-noise note demands a fresh daemon per measured run; the experiment runner had to find and `Stop-Process` it via PowerShell, and 8 orphaned `nosuchdaemon` daemons from the unit suite were still alive at 18:00 | low |
| 5 | **State save/restore** (per-origin storage, encrypted) | agent-browser `state.rs` | 40% of joblens' jobs sit behind an account wall (`ats_map_final.json`); without persisted sessions every one of those is unreachable on every run | medium |
| 6 | **Skill split: stub + on-demand corpus shipped with the binary** | agent-browser (52 + 4,537 lines), v1's lazy `interaction-skills` | v2 loads 296 lines of `SKILL.md` into every session; its registry answers `[]` for every URL. The ATS-map vendor table is the first corpus | low (packaging) |
| 7 | **Hybrid a11y + DOM snapshot** | Stagehand, agent-browser, Playwright-MCP (three votes) | v2's `ax()` already asks Chrome for the computed name; the hand-rolled `SNAPSHOT_JS` name chain is what missed labels inside shadow roots until today's `getRootNode()` fix | high |
| 8 | **`diff` as a primitive** (snapshot/text/screenshot) | agent-browser `diff.rs` | Today's chain-follower re-implemented "did the click change anything" by comparing two inspector dumps in Python — the harness computes exactly that for `click_ref` and cannot be asked for it between two arbitrary states | medium |
| 9 | **Token/decision accounting on results** | Stagehand `StagehandResultUsage` | Every efficiency claim in `experiments-2026-08-29.md` had to use CDP round trips as the proxy for "decisions" because the journal cannot record what a caller spent | medium |
| 10 | **Visible attached-tab marker** | v1's 🐴 title prefix | Small, but the headed runs today opened ten scratch windows with nothing telling the user which were the harness's | trivial |
| 11 | Schema as the single source for namespace, help, SKILL, MCP | Stagehand `protocol/schemas.ts`, agent-browser checklist | Three hand-kept surfaces already drift (observed in both reviews); not measured today | medium |
| 12 | Plugin protocol with capabilities (`credential.read`, `browser.provider`) | agent-browser `plugins.rs` | Not exercised today; the right home for Keychain auth and hosted browsers | high |

Two things from v1 that v2 already has better versions of, so they are *not* on the
list: `wait_for_network_idle` (v2's `goto` tracks in-flight requests itself) and
`dispatch_key`/`fill_input` (v2's delivery-verified keys with DOM fallback).

## 3. What today's run says about the ranking in the two reviews

- The single largest finding of the day — the daemon evicting its own client under event
  load — is on neither list and could not have been: it is a transport defect, invisible
  from a code read and only findable by running 100 postings ten-wide. **Reading a
  competitor tells you what to build; only the corpus tells you what is broken.**
- Both reviews rank the accessibility tree highest as an *architecture* change. Today's
  data ranks it lower than the resolved-action record: shadow DOM, hidden-blank pages and
  connector URLs cost more decisions than name computation did, and v2's `ax()` already
  covers the computed-name case.
- The reviews' "borrow the design, not the source" position held up in practice today:
  every fix that landed (`deepAll`, `hidden_blank`, event filter, slimming) was a few
  dozen lines against v2's own primitives, and each shipped with the measurement that
  justifies it — which is the part no borrowed source would have carried.
