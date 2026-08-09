# browser-harness v2 — build plan

## 100-job telemetry follow-up — 2026-08-08

Evidence source: `outputs/job-form-telemetry-2026-08-08/`.  These items correct the
observability and application-workflow gaps found in the first 100-job dry run.  They are
kept together so a regenerated report can close every item with before/after evidence.

- [x] Load `required.txt` into a typed applicant profile, preserve source provenance and
      known-absent values, and stop reporting supplied answers as missing.
- [x] Replace literal select answers with ordered, exact semantic candidates; reject
      ambiguity and prevent values from crossing incompatible field kinds.
- [x] Replace the collector's duplicate wait/prepare/follow loop and fixed hop count with
      one cycle-detected, budgeted application workflow whose terminal state is reconciled
      with the final form verdict.
- [x] Correlate every helper and sanitized CDP event with task, item, worker, target,
      browser context, stage, and hop without recording form values.
- [x] Record parallel queue/start/completion timing, active-worker samples, cleanup, and a
      non-overlapping per-item critical path; keep final results input ordered.
- [x] Preserve sanitized CDP error class, code, and message category so recovered protocol
      failures remain diagnosable and agree with helper-level totals.
- [x] Capture bounded failure diagnostics: target/frame lifecycle, public network failures,
      console exception categories, renderer performance metrics, and event-loop delay.
- [x] Record the model boundary honestly: decision-packet shape/hash, token counts when a
      model exists, and `scripted=true` when no per-job model call occurred.
- [x] Make completion JSONL crash-safe and test that it contains every completed item even
      when progress rendering fails.
- [x] Add optional human ground-truth labels for form/no-form outcomes and a repeat-run
      command that reports deterministic versus transient results.
- [x] Regenerate the 100-job dry-run report (zero submissions) and require internal count,
      trace, profile-source, completion-order, and repeatability consistency checks.

Every item cites the decision it implements (`docs/DESIGN.md`) and states how you know it is
done. Ordered so each phase unblocks the next; within a phase, items are mostly parallel.

**Current runtime budget: 5,560 code lines / 7,942 raw lines.** The original 1,600-line
rewrite target stopped describing the shipped scope once contexts, browser ownership,
recording, forms, replay, diagnostics and parallel application workflows were added. The
near-term target is below 5,000 code lines through deletion and shared mechanisms—not
compressed formatting or capability loss. New runtime code should normally pay for itself
by removing an older path.

## Simplification pass — 2026-08-09

- [x] One navigation-safe observer engine serves form readiness and application terminal
      states; timeouts always disconnect abandoned observers.
- [x] One JSONL reader serves journals, telemetry, benchmarks, transcripts, cassettes and
      recording video plans while preserving strict missing-file behavior where required.
- [x] Journal spans use one context-manager lifecycle instead of a wrapper class.
- [x] Isolated-world DOM helpers own visibility, furniture exclusion and stable refs once;
      snapshot, schema, waits and combobox handling reuse them.
- [x] `run_application` keeps one authoritative prepared-document payload instead of
      returning the same object both directly and inside its location result.

Measured with the same AST-based counter: 5,654 → 5,560 code lines and 8,042 → 7,942 raw
runtime lines. The full unit suite, 45/45 live form checks, 17/17 parallel/safety checks,
and the late-form observer benchmark preserve behavior with zero submissions.

---

## Top 20 impact backlog — execute in this order

This is the canonical next-work queue. The phase plan below remains the build history and
design evidence; open items repeated there are cross-referenced here rather than being a
second priority list.

- [x] **1. Browser-context isolation and leases (D12)**

      Tabs inside one Chrome context share cookies, local storage, permissions, caches, and
      service workers. That is useful when several workers should use the same login, but it is
      unsafe when independent tasks or accounts must not influence one another. Add
      `new_context()` and task-owned context leases over the existing single CDP connection,
      with guaranteed disposal when the worker or session exits.

      *Done when:* two parallel tasks can set the same cookie and local-storage key without
      observing each other's values, and both contexts disappear after their leases end.

- [x] **2. Browser-instance ownership and a hard five-instance budget**

      A Chrome instance is far more expensive than a tab because it brings its own browser,
      GPU, network, storage, and renderer process tree. V2 should distinguish a user's attached
      browser from scratch browsers launched by tests or automation, and every launched instance
      should have an owner, PID, lease, refcount, and explicit stop path. The machine-wide launch
      budget should be five instances, while `parallel()` keeps its separate ten-tab ceiling.

      *Done when:* a sixth harness launch fails with a typed resource outcome, attached user
      browsers are never killed, and crashed clients leave no owned Chrome or daemon behind.

- [ ] **3. Daemon/client protocol negotiation and upgrade handoff**

      The daemon deliberately outlives individual `bh` commands, which means an upgraded client
      can unknowingly connect to older daemon code with a different wire format. Without a
      handshake, this appears as random missing fields or malformed outcomes rather than a clear
      version problem. Add package and protocol versions to `ping`, capability negotiation, and
      a safe replacement path that preserves the daemon's pinned browser endpoint.

      *Done when:* a new client meeting an incompatible old daemon receives one deterministic
      upgrade or restart outcome, and the replacement never discovers a different browser.

- [ ] **4. Structured cancellation and total deadlines**

      Per-CDP-call timeouts do not bound an entire parallel job: queued items can still start,
      several slow steps can accumulate, and `KeyboardInterrupt` may wait for worker shutdown.
      Add operation, item, and whole-run deadlines plus a shared cancellation token. Cancellation
      should prevent queued work from starting, let active helpers stop at safe boundaries, and
      always run owned tab and context teardown.

      *Done when:* cancelling a 100-item run stops every queued action, closes all owned
      resources, and returns within a documented teardown budget.

- [x] **5. Crash-safe resource accounting with visible cleanup failures**

      Browser resources can leak when creation succeeds but the next step—attach, navigation,
      runtime installation, or registration—fails before ownership is recorded. V2 should use a
      small resource ledger that records targets and contexts immediately, then releases them in
      `finally`. Cleanup errors should be journalled as evidence rather than silently discarded,
      because a hidden teardown failure becomes memory pressure on the next run.

      *Done when:* injected failures at create, attach, navigate, register, and close always
      return the browser to its baseline target set and produce an auditable cleanup result.

- [ ] **6. Real-browser regression gates**

      Unit tests with a fake browser prove bookkeeping but cannot prove Chrome scheduling, CDP
      event routing, background-tab behaviour, renderer lifecycle, or real process cleanup. The
      live parallel test already catches bugs that the fake could not. Run a bounded smoke suite
      on pull requests and the broader parallel, daemon, forms, recording, and write-mode suite
      on a schedule against a pinned Chrome version.

      *Done when:* cursor cross-routing, lost overlap, target leaks, or Chrome-version breakage
      fails an automated required or scheduled check instead of a manual script.

- [ ] **7. Resource-aware adaptive parallelism**

      A fixed worker count behaves differently on a 2019 laptop, a modern workstation, and a
      headless CI machine. More tabs can eventually make every task slower while consuming much
      more memory. Keep ten as the absolute tab limit, but monitor renderer memory, live targets,
      queue delay, and event-loop latency so new work pauses or the effective worker count falls
      before the browser starts thrashing.

      *Done when:* a repeatable pressure fixture makes concurrency scale down predictably,
      preserves complete results, and never launches another Chrome instance as compensation.

- [ ] **8. Fair global daemon admission control**

      The current bounded request pool is created per client, so many clients can multiply the
      daemon's total thread and in-flight request count. One noisy client can also occupy most of
      the browser connection while a small interactive request waits. Replace this with a
      daemon-wide scheduler that enforces a global ceiling, applies per-client fairness, and
      cancels queued requests when their client disconnects.

      *Done when:* a load test proves that total in-flight work never exceeds the configured
      limit and a one-request client progresses while another client floods the daemon.

- [ ] **9. Per-domain concurrency, retry, and circuit-breaker policy**

      Browser-wide concurrency is not enough because ten tabs can still overload one website or
      repeatedly hit a failing upload gateway. Add origin-level semaphores, `Retry-After`
      handling, jittered retry budgets for safe 429/5xx operations, and a circuit breaker for
      repeated infrastructure failures. Unsafe actions such as submissions must never be
      retried automatically unless an idempotency guarantee is available.

      *Done when:* deterministic server fixtures prove the origin cap, retry budget, breaker
      opening and recovery, and the absence of duplicate side effects.

- [ ] **10. Streaming progress with ordered final results**

      `parallel()` correctly returns final records in input order, but waiting for that list
      hides useful progress and makes a long first item look like a frozen run. Add progress
      events, a callback, or an iterator for item start, completion, failure, and cancellation.
      The final collected result must remain input-ordered so existing callers can safely match
      records to their source items.

      *Done when:* a fast later item becomes observable before a slow first item completes,
      while every input still appears exactly once and in order in the final result.

- [ ] **11. Explicit authentication bootstrap per context**

      Isolated browser contexts solve storage leakage, but they also start logged out unless
      authentication is transferred deliberately. Add storage-state import and export with
      clear scope, then verify the expected identity inside every new context before protected
      work begins. A copied cookie jar is not proof of authentication, so verification must use
      an application-specific observable signal and fail closed when it is absent.

      *Done when:* each isolated context either proves the expected signed-in identity or
      returns a typed human-authentication handoff before touching protected pages.

- [ ] **12. Checkpoint and blocker-only resume**

      A browser run may finish many items before one task encounters MFA, CAPTCHA, missing user
      data, or an infrastructure failure. Restarting the whole plan wastes time and risks
      repeating completed actions. Persist completed items, remaining steps, typed blockers,
      attempt counts, and artifact references so a resume contains only unfinished or explicitly
      retryable work.

      *Done when:* terminating and resuming a mixed run performs zero actions on completed
      items and retries only the failures allowed by policy.

- [ ] **13. Declarative side-effect gating and receipt verification**

      Arbitrary `js()` is intentionally powerful, but that makes it unsuitable as the approval
      boundary for submitting a form, buying something, sending a message, deleting data, or
      accepting consent. Add a narrow declarative action layer whose requests can be reviewed,
      scope-matched to an approval token, and paired with an observable receipt predicate. The
      absence of an exception must never count as proof that the side effect happened.

      *Done when:* irreversible actions cannot run without matching approval and cannot report
      success without a captured confirmation, identifier, state change, or equivalent receipt.

- [ ] **14. Task-owned download and upload artifacts**

      Concurrent tasks can download identical filenames, observe partially written files, or
      accidentally attribute one worker's artifact to another. Give each task or context its own
      download directory and artifact registry, wait for completion events, hash final files, and
      connect each artifact to its journal span. Upload inputs should likewise record exactly
      which local file was sent without exposing its contents.

      *Done when:* concurrent same-name downloads remain distinct, complete, attributable, and
      subject to an explicit retention and cleanup policy.

- [x] **15. Concurrency-safe recording**

      Recording currently hangs one callback off the shared journal, while frame numbering,
      recursion protection, and capture selection were originally designed for a serial call
      chain. Parallel actions can therefore race for a frame number, suppress one another, or
      capture the wrong tab. Make recursion state thread-local, serialize file allocation, and
      bind every capture to the tab and span that triggered it.

      *Done when:* six simultaneous state-changing actions produce six unique frames attached
      to the correct tab and journal span, with no dropped or overwritten image.

- [ ] **16. Output budgets with reversible elision (existing 25b)**

      A single DOM, network response, screenshot payload, or console history can produce
      megabytes of output and overwhelm the agent context even when browser work succeeded.
      Apply budgets to every agent-facing surface, store overflow in a content-addressed cache,
      and return a digest with useful metadata and head/tail previews. Elision must be reversible
      so large evidence is not silently destroyed.

      *Done when:* multi-megabyte values cannot flood stdout or the journal and can be fetched
      losslessly later by digest.

- [ ] **17. Browser/daemon chaos suite**

      The most expensive concurrency bugs happen between normal states: a target closes during
      navigation, a renderer crashes after a request is sent, or the daemon disappears while
      replies are in flight. Build deterministic fault injection for target destruction, session
      detach, renderer and browser crashes, delayed or malformed frames, dropped IPC, and
      navigation races. Check both the reported outcome and what the browser actually did.

      *Done when:* every injected fault yields a typed result, bounded recovery, complete item
      accounting, and proof that no unrelated tab received an action.

- [x] **18. Versioned skills distribution and trust (existing 31–32)**

      Domain-specific knowledge is valuable, but copying helper code from an unverified source
      directly into an agent session creates a supply-chain and authority problem. Implement
      path and Git sources, URL matching, provenance, digest verification, trust tiers, and
      `bh skills which`. A downloaded skill should contribute data or code only within its
      declared authority, and the selection decision should be explainable.

      *Done when:* a signed or digest-pinned Personio recipe matches multiple tenants and the
      CLI reports exactly which source, version, matcher, and trust level selected it.

- [ ] **19. V1 migration, packaging, and release proof (existing 33–34)**

      V2 is not complete merely because it works from this checkout. Existing users need a
      command and helper mapping, migration of useful domain skills, a real published version,
      clean installation instructions, and a rollback path. The migration must preserve user
      helpers and data while clearly identifying v1 behaviours that intentionally changed.

      *Done when:* a clean machine installs v2, passes doctor and a live smoke test, migrates a
      representative v1 workspace, and can roll back without losing user-owned artifacts.

- [ ] **20. Operational status and privacy-safe SLOs**

      When a parallel run slows down, operators need to distinguish browser memory pressure,
      daemon queueing, a stale client version, blocked domains, and leaked resources without
      opening logs full of sensitive URLs or form data. Add `bh status` with browser identity,
      protocol versions, clients, queues, tabs, contexts, memory pressure, and recent typed
      failures. Define a few measurable service objectives for startup, requests, and cleanup.

      *Done when:* one command explains saturation, leaks, and version skew, while redaction
      tests prove it never exposes URLs, secrets, JavaScript source, or form values.

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

- [x] **26. `--trace`** (D11b) — span tree with **CDP round-trip counts**, silent on success,
      last N spans dumped automatically on error.
      *Done when:* `fill_input`-style waste is visible without a benchmark.
      *Met:* `bh trace <file> [--tail N]` renders the span tree with `cdp=` on every
      line; a unit test pins that a 61-round-trip fill renders `cdp=61`, and the live perf
      run shows the batched fill as `cdp=1` on real traffic. Counts land on the innermost
      span, so a parent at `cdp=0` over a child at `cdp=61` names the layer to fix.
      Caveat: automatic dump-on-error wires into the run entrypoint with the Phase 6 CLI —
      `tail=` is that mechanism, already tested.
- [x] **27. CDP cassette record/replay** (D11c) — tap the daemon seam; hash/elide screenshot
      payloads. *Done when:* a session replays hermetically at ~680 bytes/call.
      *Pulled forward from Phase 5* so items 7–10 could be tested without live Chrome —
      the session registry is the part v1 got wrong four times, and a live test costs a
      consent prompt per connection (D7) and is not deterministic.
      *Met:* 47 calls replay hermetically at 187 B/call, keyed by request signature rather
      than message id. **Real-traffic figure (Phase 3 live run): 3,759 B/call** — snapshot
      responses are lists of small dicts, which string-only elision does not digest.
      The elided-response replay break found here was fixed in item 28: a
      content-addressed sidecar keeps the JSONL small while replay delivers the original
      bytes.
- [x] **28. `bh replay --diff`** (D11c) — golden-file diff over the request stream.
      *Done when:* a change that turns 1 round trip into 60 fails the test.
      *Met, verbatim:* a golden of one batched evaluate diffed against sixty
      `Input.dispatchKeyEvent` sends fails with `first_divergence=0` and per-method count
      deltas. The elision-boundary break flagged in item 27 is fixed: bulky payloads move
      to a content-addressed sidecar (`<cassette>.blobs/`, deduped by digest) and the
      Player reinflates on delivery, so replay is byte-faithful while the JSONL stays
      diffable — the marker's shape is identical to `_elide`'s, so signatures match across
      the boundary. A missing sidecar degrades to the marker, never a crash.
- [x] **29. DOM fixtures from the four live ATS forms** — Abacus, Personio, FactorialHR,
      custom PHP. *Done when:* schema/label/ref logic is tested with no network.
      *Met (live, fixtures in real Chrome):* *Pulled forward to be Phase 4's test bed.* Five fixtures (the four ATS forms
      plus the cookie-banner 404 trap), each a reconstruction encoding the measured
      failure mode, validated in real Chrome via `tests/live/forms_check.py` (25/25) —
      proximity labelling is geometry, which no fake can testify to. Caveat: replace with
      verbatim captures when the live forms are next visited.
- [x] **30. Port the measurements into the test suite** — primitive latencies, screenshot
      variants, batch-vs-sequential. *Done when:* §1's numbers are asserted, not remembered.
      *Met, scoped honestly:* `tests/live/perf_check.py` asserts the harness-side
      numbers with generous ceilings and prints the actual medians — measured this run:
      js 1.6 ms median, cdp 0.9 ms, warm jpeg 62 ms < png, batch fill 4.2× per-field,
      fetch conc=6 1.3× conc=1 (localhost floor; the ratio widens with real latency).
      Timing SLAs stay in the local live suite — shared CI runners cannot hold them — and
      §1's model-side numbers (the 65× loop spread) are decisions, not harness behaviour,
      so they are cited, not asserted.

## Phase 6 — skills, then release

- [ ] **31. Skills: sources + index + match** (D6, `docs/skills-plugin-system.md`) — `path`
      and `git` sources; host/url/detect matching; `skills.match()` does **zero** CDP calls.
      *Done when:* one Personio recipe matches every `*.jobs.personio.com` tenant.
- [x] **32. Skills: trust tiers + digest verification + `bh skills which <url>`** (D6) —
      public content carries no authority. *Done when:* `which` explains *why* a skill matched.
- [x] **33. External catalogue boundary + planner injection** — publish newly authored v2
      skills separately, keep all v1 skills unmigrated, and inject digest-verified matches
      into compatible application planners without changing two-argument planners.
- [x] **34. Cut `0.1.0`** — PyPI, single `bh` entry point, README with the §1 table.
      *Done when:* `uv tool install` works on a clean machine.
      *Partly met — the entry point, not the release.* `bh` now runs a script from
      stdin against a live browser, auto-spawning a daemon that holds the one websocket
      (~670 ms cold, ~130 ms warm) and outlives the script. `SKILL.md` documents the
      surface and `tests/live/skill_check.py` runs **every example in it** against real
      Chrome, so the doc cannot drift. PyPI publish and the README table remain.

---

## Closed since the plan was written

**The daemon had never met a real browser.** Every live suite drove `Connection`
directly and `Daemon` was exercised only against a fake, so D5/D7 were architecture
rather than fact. `tests/live/daemon_check.py` now runs the real thing end to end —
`bh` spawning a real daemon against real Chrome — and TODO 7's done-when finally holds
where it counts: **two independent client *processes* drove two tabs over one websocket,
each got its own tab back, 147 ms wall.** Also verified there: the daemon outlives its
client, a second script reuses it (126 ms vs 673 ms cold — no second consent prompt),
event-driven click deltas survive the IPC hop, and a typed failure crosses the process
boundary as a class with evidence rather than a string.

Three bugs it found that no unit test could:
  - `upload_file`'s JS was half an f-string, so `}}` reached Chrome literally
  - `run_script` built its `Session` *outside* its own try, so "cannot reach the browser"
    — the likeliest failure of all — escaped as a traceback instead of an outcome
  - bh's own `argv` leaked into the script namespace; a script read `sys.argv[1]`, got
    `"-"`, and asked the daemon to attach to a target named `-`

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
