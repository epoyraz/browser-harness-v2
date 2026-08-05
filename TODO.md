# browser-harness v2 — build plan

Every item cites the decision it implements (`docs/DESIGN.md`) and states how you know it is
done. Ordered so each phase unblocks the next; within a phase, items are mostly parallel.

**Budget target: ~1,600 lines of core.** v1 is 5,736 across 13 modules. If an item cannot be
expressed in tens of lines, re-read §5 — every superseded PR in v1's history lost to a
version 5×–60× smaller.

---

## Phase 0 — foundation (nothing works until these do)

- [x] **1. Repo scaffold + `pyproject.toml`** — `harness/{core,connect,cli,ops}`, `uv` install,
      single entry point `bh`. *Done when:* `uv run bh --version` prints.
- [x] **2. CI: pytest + ruff on every push** — *Done when:* a red test blocks the run.
- [x] **3. `Outcome` and typed errors** (D11) — `ok`, `class` (closed enum), `detail`,
      `observed`, `retryable`, `id`. Python exceptions derive from the class and carry the
      payload. *Done when:* `NavigationFailed(...).landed` works and no code path stringifies
      an error to re-parse it later.
- [x] **4. Test policy: no module without a test file** (§2.2) — enforced in CI.
      *Done when:* adding `harness/core/foo.py` with no `tests/unit/test_foo.py` fails CI.
- [x] **5. Journal writer** (D11b) — append-only JSONL per session; every entry carries
      `ts`, `id`, `kind`. *Done when:* a run produces a file whose entries can be ordered
      against each other.

## Phase 1 — transport and session (the part v1 got wrong)

- [x] **6. IPC transport** — AF_UNIX on POSIX, TCP loopback + token on Windows, as **one
      `platform.system()` branch**, never a strategy abstraction (§6: 13 rejected PRs).
      Carry v1's macOS 104-byte `sun_path` constraint and the runtime/tmp dir split.
      *Done when:* both platforms pass the same test file.
- [x] **7. Daemon: one CDP websocket, per-request session routing** (D5) —
      *Done when:* two clients drive two tabs concurrently over one connection.
      *Met:* six clients over two tabs, 300 ms each, finish in <0.9 s (serialised would be
      1.8 s) with `max_in_flight > 1`, and each gets its own target back.
- [x] **8. Session registry `{targetId → sessionId}`** (D1) — lazy attach, per-target
      locking. *Done when:* issue #375's subagent case works: N clients, N tabs, no fighting.
      *Met:* there is no `current_tid` at all — the target is a parameter, so there is no
      shared cursor to fight over. Eight threads racing one target produce one session.
- [x] **9. Exactly ONE `ready_session(target_id)`** (D1, regression class A) — every path goes
      through it: initial attach, switch, lazy attach. *Done when:* grep finds one function
      that enables domains, and a test asserts no other path can produce a session.
      *Met:* `test_one_function_enables_domains` greps the shipped tree; mutation-tested by
      adding a stray `Page.enable` to `cdp.py`, which fails it. Raw `Target.attachToTarget`
      over the wire is routed to `ready_session()` rather than left as a bypass.
- [x] **10. Typed session states** (D11a, #352) — `attached`, `target_missing`,
      `session_stale`, `renderer_unresponsive`, `browser_disconnected`; one
      `ensure_live()` boundary. *Done when:* zero string-matching on CDP prose anywhere
      in the tree.
      *Met, with one honest qualification:* prose is read in exactly one function,
      `cdp.classify()`, which maps it to the closed enum. It cannot be zero — CDP returns
      `-32000` for nearly everything — but it is one place with one test instead of every
      recovery site. Staleness is normally learned *before* any failure, from
      `detachedFromTarget` / `targetDestroyed` / `targetCrashed`; v1 subscribed to none of
      these, which is why prose was its only signal.

## Phase 2 — connection lifecycle (where v1's complexity actually lives)

- [ ] **11. Endpoint discovery as a ranked strategy list** (D8) — explicit URL, then
      liveness-probed profile candidates. Not a nested fallback ladder.
      *Done when:* each strategy reports which one won and why the others declined.
- [ ] **12. Endpoint binding with trust mode** (D10) — `pinned` never widens scope, ever;
      `discover` is opportunistic. *Done when:* a pinned daemon respawned without its env
      **refuses** rather than attaching to the user's daily-driver Chrome (#479).
- [ ] **13. Recovery fails closed** (D10, observed live) — a dead pinned target returns an
      error naming it; never silently substitutes `pages[0]`.
      *Done when:* the stale-session path cannot reach an unrelated tab.
- [ ] **14. Chrome compatibility, carried over intact** (§7) — M144 per-connection consent
      (one held connection, never a retry loop — D7), M147 `/json/*` 404 → `DevToolsActivePort`
      fallback, stale-port liveness check, profile enumeration + override instead of 30
      hardcoded vendor paths. *Done when:* `--doctor` classifies each failure by type (D11).

## Phase 3 — primitives (the agent-facing surface)

- [ ] **15. `cdp()` and `js()`** — per-call timeout as an **argument, not an env var** (§6);
      `replMode` so top-level `await` works (D14). *Done when:* `js("await fetch(...)")` runs.
- [ ] **16. `goto()`** (D11) — returns `requested` **and** `landed`; raises
      `NavigationFailed` when CDP reports `errorText` or the tab lands on `chrome-error://`.
      *Done when:* a 404 cannot be reported as a title.
- [ ] **17. Actions return a delta** (D11) — `click_*` reports url change, new tab, DOM node
      delta, dialog opened + text. *Done when:* clicking a button that opens a modal reports
      the modal, not silence.
- [ ] **18. Injected page runtime** (D13) — `Page.addScriptToEvaluateOnNewDocument` installs
      the ref registry and a MutationObserver on every document.
      *Done when:* snapshot refs survive a navigation.
- [ ] **19. Event-driven waits** (D13) — `Runtime.addBinding` + `Page.lifecycleEvent` replace
      300 ms polling. *Done when:* wait overshoot drops from ~153 ms median to <10 ms.
- [ ] **20. `snapshot()` / `click_ref()`** (D4) — interactive elements with exact coordinates
      from `getBoundingClientRect`. *Done when:* 450 elements in <10 ms.
- [ ] **21. `capture_screenshot()`** (D4) — JPEG, `clip.scale=0.5` → CSS pixels on any
      display; `max_dim` computes scale rather than post-resizing.
      *Done when:* output px == CSS viewport px, ~150 ms.

## Phase 4 — the batching surface (where the speed is)

- [ ] **22. `fetch_all(urls, concurrency=5)`** (D0) — in-page, session-authenticated, bounded,
      retries 429/5xx, returns **attempted / succeeded / failed**.
      *Done when:* it cannot silently return 163 of 300.
- [ ] **23. `form_schema()`** (D15) — label chain **plus proximity fallback**; `required`,
      `options`, `autocomplete`; marks placeholder options and ARIA comboboxes; excludes
      buttons, files, cookie/search furniture; returns a **form-identity verdict**.
      *Done when:* the Abacus fixture yields "Vorname *", not `customeraddressshoppervorname`.
- [ ] **24. `fill_form(plan)`** (D15) — one write, focus→input→change→blur, per-field
      `ok`/`got`/`want`; accepts a **label** for long option lists and resolves it in-page.
      *Done when:* 249 phone prefixes cannot silently select Spain.
- [ ] **25. `set_value()` + keystroke opt-in** (D3) — one round trip by default.
      *Done when:* a 2,000-char field is one call, not 6,000.

## Phase 5 — observability, replay, regression tests

- [ ] **26. `--trace`** (D11b) — span tree with **CDP round-trip counts**, silent on success,
      last N spans dumped automatically on error.
      *Done when:* `fill_input`-style waste is visible without a benchmark.
- [x] **27. CDP cassette record/replay** (D11c) — tap the daemon seam; hash/elide screenshot
      payloads. *Done when:* a session replays hermetically at ~680 bytes/call.
      *Pulled forward from Phase 5* so items 7–10 could be tested without live Chrome —
      the session registry is the part v1 got wrong four times, and a live test costs a
      consent prompt per connection (D7) and is not deterministic.
      *Met:* 47 calls replay hermetically at 187 B/call, keyed by request signature rather
      than message id. Caveat: measured against the in-process fake, whose payloads are
      smaller than Chrome's; the real figure needs a live recording in Phase 2.
- [ ] **28. `bh replay --diff`** (D11c) — golden-file diff over the request stream.
      *Done when:* a change that turns 1 round trip into 60 fails the test.
- [ ] **29. DOM fixtures from the four live ATS forms** — Abacus, Personio, FactorialHR,
      custom PHP. *Done when:* schema/label/ref logic is tested with no network.
- [ ] **30. Port the measurements into the test suite** — primitive latencies, screenshot
      variants, batch-vs-sequential. *Done when:* §1's numbers are asserted, not remembered.

## Phase 6 — skills, then release

- [ ] **31. Skills: sources + index + match** (D6, `docs/skills-plugin-system.md`) — `path`
      and `git` sources; host/url/detect matching; `skills.match()` does **zero** CDP calls.
      *Done when:* one Personio recipe matches every `*.jobs.personio.com` tenant.
- [ ] **32. Skills: trust tiers + digest verification + `bh skills which <url>`** (D6) —
      public content carries no authority. *Done when:* `which` explains *why* a skill matched.
- [ ] **33. Migration + docs** — generate frontmatter for v1's 105 skill files, publish as the
      first community source, write the v1→v2 mapping.
- [ ] **34. Cut `0.1.0`** — PyPI, single `bh` entry point, README with the §1 table.
      *Done when:* `uv tool install` works on a clean machine.

---

## Open questions

**Resolved:** the process model. Measured (D7): a second connection to an *already
authorised* Chrome is denied a fresh consent prompt every time — 0/6 succeeded, and Chrome
serialises the prompts. So **one daemon per browser endpoint, multiplexing N clients**;
items 7–8 are unblocked. Per-client daemons would put a modal in front of every subagent.

**Still open, blocking Phase 3's shape:** the gating layer. Arbitrary code cannot be
approved, so if v2 wants human-in-the-loop for hard-to-reverse actions it needs a
*declarable* action layer above the REPL. "Containerize it" is v1's answer, not a design.
See `docs/DESIGN.md` §9.

## Not doing

Transport abstraction · env-var knobs for per-call behaviour · domain skills in-tree ·
plugin/marketplace packaging · enterprise ops surface · re-litigating the heredoc CLI ·
request batching · subresource blocking · WebDriver BiDi · implicit focus management.
Each is evidenced in `docs/DESIGN.md` §6.
