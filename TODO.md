# browser-harness v2 — build plan

Every item cites the decision it implements (`docs/DESIGN.md`) and states how you know it is
done. Ordered so each phase unblocks the next; within a phase, items are mostly parallel.

**Budget target: ~1,600 lines of core *code*.** Restated after Phase 1: measured by AST
(code lines only — docstrings and comments excluded, since they carry the why that stops
v1's regressions from being re-litigated). v1 is 5,736 raw across 13 modules. If an item
cannot be expressed in tens of lines, re-read §5 — every superseded PR in v1's history lost
to a version 5×–60× smaller.

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

- [x] **11. Endpoint discovery as a ranked strategy list** (D8) — explicit URL, then
      liveness-probed profile candidates. Not a nested fallback ladder.
      *Done when:* each strategy reports which one won and why the others declined.
      *Met, validated live:* on this machine `/json/*` is 404 (M147) and the profile
      strategy still resolved `ws://…9222/devtools/browser/…` from `DevToolsActivePort`
      with no HTTP at all; `--doctor` printed all three verdicts. Probes are
      websocket-free — a ws "liveness check" would cost a consent prompt per probe (M144).
- [x] **12. Endpoint binding with trust mode** (D10) — `pinned` never widens scope, ever;
      `discover` is opportunistic. *Done when:* a pinned daemon respawned without its env
      **refuses** rather than attaching to the user's daily-driver Chrome (#479).
      *Met:* an explicit env URL *is* a pin, pins persist per daemon name so a respawn
      without env stays pinned, and the #479 test plants a live discoverable "daily driver"
      next to a dead pin — resolution raises naming the pin and never touches the live one.
      `BH_TRUST=discover` is the one deliberate way out.
- [x] **13. Recovery fails closed** (D10, observed live) — a dead pinned target returns an
      error naming it; never silently substitutes `pages[0]`.
      *Done when:* the stale-session path cannot reach an unrelated tab.
      *Met:* mostly by construction — there is no `attach_first_page()` and no fallback
      path to hold wrong. The daemon test destroys the named target with a second tab
      present and asserts the error names the dead target and the other tab's attach
      count stays zero.
- [x] **14. Chrome compatibility, carried over intact** (§7) — M144 per-connection consent
      (one held connection, never a retry loop — D7), M147 `/json/*` 404 → `DevToolsActivePort`
      fallback, stale-port liveness check, profile enumeration + override instead of 30
      hardcoded vendor paths. *Done when:* `--doctor` classifies each failure by type (D11).
      *Met, two caveats:* (1) doctor classifies `endpoint_unreachable` / `endpoint_404` /
      `no_browser_window` / `scope_refused`, and reports an M147-hidden window count as
      *unknown* instead of guessing — `permission_pending` still requires an observed
      prompt, which only the daemon's own connect can observe (rule 1). (2) v1's Chrome
      auto-launch and chrome://inspect auto-open are NOT carried into core; doctor prints
      the instruction instead. Revisit at item 34 if the install story needs it.

## Phase 3 — primitives (the agent-facing surface)

- [x] **15. `cdp()` and `js()`** — per-call timeout as an **argument, not an env var** (§6);
      `replMode` so top-level `await` works (D14). *Done when:* `js("await fetch(...)")` runs.
      *Met (live, scratch-profile Chrome on this machine):* `js("const r = await fetch(...); r.status")` → 200.
      A value-less result raises `NotSerializable` instead of v1's silent None; note Chrome
      serialises a DOM node to `{}` under `returnByValue`, so that path fires only for
      genuinely unserialisable results.
- [x] **16. `goto()`** (D11) — returns `requested` **and** `landed`; raises
      `NavigationFailed` when CDP reports `errorText` or the tab lands on `chrome-error://`.
      *Done when:* a 404 cannot be reported as a title.
      *Met (live, scratch-profile Chrome on this machine):* refused port → `net::ERR_CONNECTION_REFUSED` typed; empty-body 404 →
      `NavigationFailed` with `landed=chrome-error://chromewebdata/`. Bonus find: Chrome
      rejects low ports as `ERR_UNSAFE_PORT` before connecting — a distinct prose, same
      class, which is the point of classifying once.
- [x] **17. Actions return a delta** (D11) — `click_*` reports url change, new tab, DOM node
      delta, dialog opened + text. *Done when:* clicking a button that opens a modal reports
      the modal, not silence.
      *Met (live, scratch-profile Chrome on this machine):* a mutate click reports `dom_mutations=7`, a `target=_blank` click reports the
      new targetId, and a real blocking `confirm()` is survived: `Input.dispatchMouseEvent`
      does not ACK while the dialog is up, so a dispatch timeout with a dialog pending is
      reported as a successful click that opened a dialog — captured, auto-dismissed
      (accept=False default), page responsive after.
- [x] **18. Injected page runtime** (D13) — `Page.addScriptToEvaluateOnNewDocument` installs
      the ref registry and a MutationObserver on every document.
      *Done when:* snapshot refs survive a navigation.
      *Met (live, scratch-profile Chrome on this machine):* after `goto()` the runtime is present on the new document and a fresh
      snapshot's refs click. Refs survive by *reinstallation*, not by luck — a ref minted
      before the navigation is dead with the document that owned it.
- [x] **19. Event-driven waits** (D13) — `Runtime.addBinding` + `Page.lifecycleEvent` replace
      300 ms polling. *Done when:* wait overshoot drops from ~153 ms median to <10 ms.
      *Met (live, scratch-profile Chrome on this machine):* overshoot **0.11 ms** (lifecycle event wakes a condition variable).
      `Page.setLifecycleEventsEnabled` lives in `ready_session()` with the domain enables —
      a session without lifecycle events silently breaks every wait, so it is session setup.
- [x] **20. `snapshot()` / `click_ref()`** (D4) — interactive elements with exact coordinates
      from `getBoundingClientRect`. *Done when:* 450 elements in <10 ms.
      *Met (live, scratch-profile Chrome on this machine):* 444 elements in **2.6 ms in-page** (26.8 ms round trip). Coordinates are
      viewport CSS px — exactly what `Input.dispatchMouseEvent` takes.
- [x] **21. `capture_screenshot()`** (D4) — JPEG, `clip.scale=0.5` → CSS pixels on any
      display; `max_dim` computes scale rather than post-resizing.
      *Done when:* output px == CSS viewport px, ~150 ms.
      *Met (live, scratch-profile Chrome on this machine):* `clip.scale = 1/devicePixelRatio` (the general form of the 0.5 that was
      measured on one Retina display): dpr=2, css=1200 → output 1200 px, **126 ms warm**.
      The first shot on a fresh renderer paid ~1 s of raster warm-up — worth knowing.

## Phase 4 — the batching surface (where the speed is)

- [x] **22. `fetch_all(urls, concurrency=5)`** (D0) — in-page, session-authenticated, bounded,
      retries 429/5xx, returns **attempted / succeeded / failed**.
      *Done when:* it cannot silently return 163 of 300.
      *Met (live, fixtures in real Chrome):* 27/30 in 349 ms via a 6-worker in-page pool; the three 500-once URLs won by
      in-page retry, the three 404s came back typed `http_error` with url+status, and a
      results array shorter than the url list is *counted* as failures. Found en route:
      under replMode a bare async IIFE's resolved value serialises to `{}` — the template
      must be a top-level `await`, and a unit test pins that.
- [x] **23. `form_schema()`** (D15) — label chain **plus proximity fallback**; `required`,
      `options`, `autocomplete`; marks placeholder options and ARIA comboboxes; excludes
      buttons, files, cookie/search furniture; returns a **form-identity verdict**.
      *Done when:* the Abacus fixture yields "Vorname *", not `customeraddressshoppervorname`.
      *Met (live, fixtures in real Chrome):* the Abacus fixture yields exactly "Vorname *" (geometric proximity — the whole
      markup chain resolves nothing there), star-in-text means required, "Bitte wählen"
      selects are marked `placeholder_first`, the ARIA combobox is visible with
      `needs_interaction`, the file input is excluded but counted, and the cookie-banner
      404 page reads as NOT a form.
- [x] **24. `fill_form(plan)`** (D15) — one write, focus→input→change→blur, per-field
      `ok`/`got`/`want`; accepts a **label** for long option lists and resolves it in-page.
      *Done when:* 249 phone prefixes cannot silently select Spain.
      *Met (live, fixtures in real Chrome):* 7 Abacus fields filled and verified in ONE evaluate, 5 ms. "Suisse (+41)"
      resolves among 249 options; "Atlantis" is `no_option_match` with candidates and
      leaves the select untouched — an index pick is structurally impossible. `Outcome`
      grew a typed `failures` list so aggregate callers branch on classes, not report
      strings.
- [x] **25. `set_value()` + keystroke opt-in** (D3) — one round trip by default.
      *Done when:* a 2,000-char field is one call, not 6,000.
      *Met (live, fixtures in real Chrome):* 2,000 chars in one evaluate; `keystrokes=True` is ONE `Input.insertText` for
      the whole string (real input events, still not per-character — v1 spent 61 round
      trips on 20 chars).
- [ ] **25b. Output budget + reversible elision** (D0/D4; headroom-style, see
      github.com/headroomlabs-ai/headroom) — every agent-facing surface (REPL stdout,
      `cdp()`/`js()` returns, console/network reads) is capped; an over-budget payload is
      spilled to a content-addressed store and replaced by digest + head/tail preview;
      `fetch(digest)` returns the original in one call. Adopt the *reversible* pattern,
      not the middleware compressor: shaping output at the source stays lossless, and a
      heuristic compressor rewriting page data would reintroduce silent-wrong-value bugs.
      *Done when:* an accidental `print(getFullAXTree)` cannot flood a transcript — the
      preview + digest lands instead, and the full payload is one call away.

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
      than message id. **Real-traffic figure (Phase 3 live run): 3,759 B/call** — snapshot
      responses are lists of small dicts, which string-only elision does not digest.
      Known issue for item 28: an elided *response* is handed to the replaying client as a
      digest, so code that decodes it (screenshot base64) breaks under replay — the elision
      boundary belongs at --diff comparison time, not at response-delivery time.
- [ ] **28. `bh replay --diff`** (D11c) — golden-file diff over the request stream.
      *Done when:* a change that turns 1 round trip into 60 fails the test.
- [x] **29. DOM fixtures from the four live ATS forms** — Abacus, Personio, FactorialHR,
      custom PHP. *Done when:* schema/label/ref logic is tested with no network.
      *Met (live, fixtures in real Chrome):* *Pulled forward to be Phase 4's test bed.* Five fixtures (the four ATS forms
      plus the cookie-banner 404 trap), each a reconstruction encoding the measured
      failure mode, validated in real Chrome via `tests/live/forms_check.py` (25/25) —
      proximity labelling is geometry, which no fake can testify to. Caveat: replace with
      verbatim captures when the live forms are next visited.
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
