---
name: browser-harness-v2
description: "Browser control over CDP via `bh` — typed outcomes, one-pass form fill, snapshot+vision perception, recording and video. Use for web interaction, scraping, testing, or filling application forms."
---

# browser-harness v2

Direct browser control over CDP. You write Python; it runs in a live session with helpers
already bound.

```bash
bh <<'PY'
goto("https://example.com")
print(page_text()[:400])
PY
```

The first call spawns a daemon that holds **one** websocket to Chrome and outlives your
script (~670 ms cold, ~130 ms warm). Later scripts reuse it. Never open your own websocket:
a second connection to an already-authorised Chrome is denied a consent prompt every time,
so it would put a modal in front of every run.

Keep long waits, pilot/full execution, validation, and the final compact result inside one
tool call; never wake the model to poll or inspect partial logs. Stop on a technical failure
with minimal evidence, and never auto-retry a semantic failure. This removed 37 polling
decisions while preserving all four blockers on the 23-form corpus.

## When not to use it

If a plain HTTP request can read it — a public page, an API, docs — use `curl`. Reach for
the browser when the task needs interaction, the user's logged-in session, JS rendering, or
a bot-protected page.

## Everything returns evidence

This is the part that differs most from other harnesses. **No helper reports success by
staying silent.** Check what you get back; do not assume.

```python
goto("https://x.test/careers")     # -> {"requested": ..., "landed": ...}
                                   # raises NavigationFailed on a 404 or chrome-error://
click_ref("e12")                   # -> {"navigated", "url_after", "dom_mutations",
                                   #     "new_targets", "dialog"}
fill_form(plan)                    # -> Outcome: .ok, .observed{attempted,succeeded,failed},
                                   #    .value = per-field ok/got/want, .failures = typed
```

Failures raise typed errors carrying their evidence — branch on the class, never on the
message text:

```python
from harness.core.outcome import Class, HarnessError
try:
    goto(url)
except HarnessError as e:
    if e.cls is Class.NAVIGATION_FAILED:
        print(e.observed["landed"])          # e.g. chrome-error://chromewebdata/
    if e.retryable: ...
```

Classes you will actually see: `navigation_failed`, `js_exception`, `not_serializable`,
`element_gone`, `no_option_match`, `needs_interaction`, `value_rejected`, `not_a_form`,
`http_error`, `timeout`, `target_gone`, `session_stale`, `partial`.

`value_rejected` is the one people misread: nothing threw. The write executed and the
control refused or rewrote it — a phone mask snapping back to `+41`. The fix is a
different write `mode`, not a different script. (`js_exception` means a real throw.)

## Read the page — two channels, one index

Perception has two channels and neither is the default. **Structure** is ~18× cheaper and
sees things vision cannot; **vision** sees things structure cannot. `see()` returns both,
sharing one index.

```python
snapshot()      # interactive elements: ref, tag, name, x, y, w, h  (~450 in ~3 ms)
see("/tmp/s.jpg")   # the same elements PLUS a screenshot with every ref drawn on it
page_text()     # rendered innerText, truncated
form_schema()   # {verdict, fields, files} — labels, required, options, refs
capture_screenshot("shot.jpeg", max_dim=800)   # a plain frame, no marks
```

`see()` writes a frame with a labelled box over every element, then hands you the element
list. Read the image to *decide*, use the `ref` to *act* — so you never estimate a
coordinate off a picture. Measured: 5 elements marked in 126 ms, 11.8 KB.

**When to spend the tokens.** A form schema is ~78 tokens; the marked frame of the same
page is ~1,390. Reach for `see()` when structure is not enough:

| Vision catches what structure misses | Structure catches what vision misses |
|---|---|
| a control that is *visually* a 1×1 nothing — the Select2 decoy read back byte-identical and submitted nothing | 249 collapsed `<option>`s — you cannot see a closed dropdown |
| a bot wall: DataDome renders an empty DOM, so `page_text()` is `""` and the page looks broken | `hidden_control` — the real `<select>` behind a widget has **no box to draw**, so it is invisible to the eye |
| layout, overlap, what is actually on top | exact coordinates, `required`, `autocomplete`, option lists |
| a modal you did not know opened | the ref that survives a re-render |

The honest rule: **start with structure, escalate to `see()` the moment something does not
add up** — a fill that verified but looks wrong, a page that reads as empty, a widget with
no obvious value. `marks=False` gives a clean frame when a human will look at it.

## Read the page (reference)

```python
snapshot()      # interactive elements: ref, tag, name, x, y, w, h  (~450 in ~3 ms)
page_text()     # rendered innerText, truncated
form_schema()   # {verdict, fields, files} — labels, required, options, refs
capture_screenshot("shot.jpeg", max_dim=800)
```

`snapshot()` gives every element a **ref** and exact viewport-CSS coordinates. Prefer refs
over writing your own selectors. Screenshots are a last resort: a schema is ~175 tokens
where a screenshot of the same form is ~3,200.

Invisible elements are skipped — **except file inputs**, which carry `hidden_control:
true` instead. A file input is never clicked (that opens a native picker with no CDP way
back out), so visibility says nothing about whether it is usable, and every dropzone UI
hides the real one. Take the `hidden_control` ref, not the visible decoy beside it.

## Act

```python
click_ref(ref) / click_at(x, y)      # coordinate clicks pass through iframes + shadow DOM
press_key("Enter") / scroll(600)      # named keys; for text use set_value
set_value(ref, text)                  # one round trip, any length
set_secret_from_keychain(ref, service=..., account=...)  # password-safe; never echoes value
upload_file(ref, "/path/cv.pdf")      # mapping + Outcome attrs; no OS picker
js("await fetch('/api').then(r=>r.json())")   # replMode: top-level await works
cdp("Target.getTargets")              # raw escape hatch, always available
```

The browser is **dry-run by default with no submit override**. Submit controls and Enter
inside a form raise `side_effect_refused`; form methods, mutating `fetch`/XHR, and beacons
are blocked before page script can use them. Normal GET navigation, inspection, filling,
uploads, and read-only API calls still work. This is a safety boundary, not a prompt
convention: an application cannot be sent through these helpers.

The sole scoped side-effect is `click_auth_ref(ref)`: it permits one request only when the
current UI is recognisably account/login/recovery UI, contains identity/password controls,
and contains neither entered files nor application text. It cannot authorize an application
submit. Use it for an explicitly approved account action after filling secrets from Keychain.

Before account creation, call `account_credential_status(url, email)`. Only when `stored`
is false may `ensure_account_credential(url, email)` generate a unique password; it stores
the secret in macOS Keychain and returns metadata only. Repeated calls are idempotent and
never overwrite an existing credential.

**`set_value` has three tiers, and the default is a bet.** Measured on an instrumented
page:

| `mode` | round trips | `isTrusted` | fires keydowns | use when |
|---|---|---|---|---|
| `"value"` (default) | 1 | false | no | ordinary inputs — most fields |
| `"insert"` | 2 | true | no | the page checks `isTrusted` |
| `"type"` | ~3N | true | **yes** | typeahead pickers, incremental masks |

A one-shot write left a keystroke typeahead's dropdown **empty**; so did `insert`. Only
`type` opened it. If a field has an autocomplete dropdown or formats as you type, use
`mode="type"`.

`insert` and `type` focus the field themselves — they go to whatever the renderer
considers focused, so an unfocused write lands somewhere else entirely and reports
nothing. You do not need to click first.

## Fill a whole form in one decision

The biggest win available. Read the schema once, decide once, write once — 45 fields across
six production ATS forms cost **2 CDP calls**, where field-by-field cost 2,321.

```python
goto(url)
prepared = prepare_application()         # guard + metadata + schema + file refs
if not prepared["is_application"]:
    raise RuntimeError("no application form found")
s = prepared["schema"]
by = {f["label"]: f for f in s["fields"]}
out = fill_form([
    {"ref": by["First name *"]["ref"], "value": "Enes"},
    {"ref": by["Country"]["ref"],      "label": "Schweiz"},   # selects take a LABEL
    {"ref": by["Phone *"]["ref"], "value": "+41 79 …", "mode": "insert"},  # see below
])
if not out.ok:
    for f in out.failures:
        print(f.cls.value, f.observed)
```

`prepare_application()` replaces separate URL, title, language, schema,
file-input and apply-link calls. It returns `schema`, `url`, `title`, `language`,
`file_inputs`, `apply_link`, `target_id`, `context`, and `contexts_checked`. A valid main
form stops immediately; `is_application` also recognises substantial JavaScript-button
forms. Iframe discovery runs only when the main document has no
substantial form. On the 23-page offline production corpus this cut public helper calls
from 184 to 23 and CDP calls from 322 to 92 with identical schemas on every page.

A step may carry its own **`mode`** (`value` default, `insert`, `type`). The batch stays
one write; the moded fields cost a round trip each and run after it, but they travel in
the same plan and come back in the same plan-ordered report. Without this, one masked
field forced you to abandon `fill_form` and hand-roll `set_value` per field — the batching
win discarded on exactly the forms that need it.

Things the schema tells you that matter:

- **`label`** is resolved by markup *and* by geometry. Some ATSs name fields
  `customeraddressshoppervorname` and put "Vorname *" in the table cell to the left.
- **`placeholder_first`** — the select opens on "Bitte wählen"; never pick `options[0]`.
- **selects take `label`, not an index.** 249-option country lists are common and picking
  by position silently selected the wrong country. No match returns `no_option_match` with
  candidates and leaves the field untouched.
- **`needs_interaction`** — an ARIA combobox built from divs. It has no value to set;
  use `select_option(ref, label)` (below), not `fill_form`.
- **`hidden_control`** — the real `<select>` behind a widget. Fill this one; the visible
  1×1 decoy accepts writes and submits nothing.
- **`verdict.is_form`** — a page can render fine and be a cookie banner plus a site search.

## Wait for a condition, never for a guess

```python
wait_for("#results", state="visible", timeout=15)   # present | visible | gone
wait_lifecycle("networkIdle")                        # document-level events
wait_for_application_state()                         # form | usable_ui | account_wall |
                                                     # bot_wall | stable_failure
```

`wait_for` arms a MutationObserver in the isolated world and wakes on the mutation — an
element that appears at 1800 ms returns at ~1720 ms, and one that never appears is a typed
`timeout`, not a silent pass. **Reach for this instead of `time.sleep`**: a guessed sleep is
slower than it needs to be when the page is fast and wrong when the page is slow.

`state="visible"` is the default on purpose — a node that exists with no box is exactly the
decoy that produced a verified write to nothing.

For client-rendered application pages, use `wait_for_application_state()` after
navigation instead of trusting `load`. A valid title with an empty body is transient;
strong terminal states return immediately, ordinary UI must stay quiet briefly, and a
genuinely empty page must remain stable longer before it becomes `stable_failure`.

To advance from a posting without branching on how the ATS implements Apply:

```python
prepared = prepare_application()
if not prepared["is_application"]:
    followed = follow_application(prepared)  # same tab, link, in-page reveal, or new target
    prepared = prepare_application()
```

For a complete bounded discovery-and-fill workflow, keep navigation, terminal-state
reconciliation, target following, cycle detection and planning inside one call:

```python
result = session.run_application(
    url,
    planner=lambda schema, language: (plan, audit),
    hop_budget=6,
)
print(result["stage"], result["fill"])
```

The planner may return a plan or `(plan, audit)`. There is no submit operation in this
workflow and the browser dry-run boundary remains active. Select steps may use ordered
exact semantic equivalents: `{"ref": ref, "labels": ["8+", "7+ Jahre"]}`. Add
`"interaction": "select"` for an ARIA combobox; `fill_form()` routes it through the
verified widget interaction while keeping one aggregate typed outcome.

`follow_application()` makes a newly opened browser target current before returning and
falls back to a discovered application URL when a click is observably inert.

## Cross-origin iframes

```python
for f in frames():
    t = session.tab(f["target_id"])     # attach to the iframe itself
    print(t.page_text()[:200])
```

An out-of-process iframe is a separate CDP target: no DOM call on the parent can see into
it, and `Target.getTargets` does not even list it. Measured live — a SmartRecruiters posting
behind DataDome had `body.innerText.length === 0` and 10 nodes, with the whole real page
inside a `geo.captcha-delivery.com` iframe. Without `frames()` that page reads as broken
rather than as bot-walled. Same-site iframes stay in the parent and are reported as
`kind: "same-document"` — reach those through `js()` and `contentDocument`.

## Write your own helpers

The harness is meant to be extended by whoever is using it. Put functions in
`~/.browser-harness/helpers.py` (or `./bh_helpers.py`, or `$BH_HELPERS`) and they are in
scope in every script from the next run on:

```python
# ~/.browser-harness/helpers.py — no imports needed
def apply_and_verify(url, plan):
    goto(url)
    out = fill_form(plan)
    return {"ok": out.ok, "landed": js("location.href")}
```

The file runs with the script namespace as its globals, so `goto()`, `snapshot()`,
`fill_form()`, `see()` are all already there. `bh helpers --init` writes a starter.
A broken helper file warns on stderr and is skipped — it never costs you the browser.

## Recording and video

```bash
BH_RECORD=1 bh <<'PY' ... PY     # a frame per state-changing action
bh recordings                     # newest first
bh video                          # render the newest to mp4
bh stats                          # what you use, and what fails
```

A recording is a folder with `session.jsonl` — **the journal itself**, with `frame` added to
the calls that produced one — plus the JPEGs. So `bh trace <recording>/session.jsonl` works
unchanged, and the video's timing is the real measured gap between actions, clamped to
0.6–3 s and reported when it was.

For continuous motion rather than action snapshots, use the compositor stream:

```python
path = start_screencast(quality=88, max_width=1440, max_height=1000)
# drive the tab normally
stop_screencast()
```

`bh video <path>` encodes its timestamped JPEG frames to H.264. This path is autonomous
and keeps raw frame metadata in `frames.jsonl`. For a native WebM MediaStream instead,
`bh recording-extension` prints the bundled unpacked Chrome extension. Load it in Chrome
for Testing and invoke its action once to start and once to stop; Chrome requires those
real user gestures for `tabCapture`. The file is written locally to
`Downloads/browser-harness-recordings/` and no capture data is sent over the network.

`bh stats` reads those journals and ranks failures by outcome class. It carries no URLs, no
arguments and no JS source — only helper name, class, duration and round-trip count.

## Comboboxes

```python
select_option(ref, "Schweiz")     # ARIA combobox OR native <select> — same call
```

`form_schema` marks div-based widgets `needs_interaction` and `fill_form` refuses them —
a div has no value to set, and writing one throws `Illegal invocation`. `select_option`
is the way through: it opens the widget, finds the option, clicks it, and **verifies the
widget changed** rather than merely that something was clicked.

It handles the shapes real ATSs use: popups portalled to `<body>` far from the combobox,
and typeaheads that render **no options at all** until you type (it detects that and types
the label to filter first). A native `<select>` is delegated to `fill_form`, so you never
have to branch on `kind`.

No match returns `no_option_match` **with candidates** and leaves the widget untouched —
never "the first option", which is how the wrong country gets selected. It also closes the
popup on failure, so a failed selection does not swallow your next click.

## Tabs

```python
t = new_tab("https://example.com")     # creates, attaches, makes current
use_tab(t.target_id)                   # switch
targets()                              # list page targets
close_tab()
```

Your current tab is client-local: two scripts running at once cannot steal each other's.

## Parallel tab work

```python
def inspect(url):
    goto(url)
    return {"url": url, "title": js("document.title")}

records = parallel(urls, inspect, workers=5, isolated=True, timeout=120,
                   progress=lambda done, total, record: print(done, total))
print(summarise(records))
```

Each record also carries privacy-safe `telemetry`: item/worker identity, target/context,
queue time, duration, completion offset and active-worker counts. `events=` receives
start/completion events immediately; `item_id=` supplies a stable safe identifier. These
same fields correlate helper and sanitized CDP journal events without recording item data.

`parallel()` uses worker tabs inside the **same Chrome instance**. It defaults to 8 tabs,
never exceeds 10, returns one record per input in input order, and closes every tab it
opened. Pass `reuse_tabs=False` when each item needs a fresh tab; each old tab is closed
before that worker claims another item, so a 100-item run still peaks at `workers` tabs.
`timeout=` and `CancelToken` stop queued work at safe item boundaries. Active helpers keep
their own explicit CDP timeout. A progress callback sees completions immediately without
changing final input order.

By default, tabs share cookies, local storage, permissions, and service workers. Pass
`isolated=True` to give each worker an owned incognito context while keeping one Chrome
process; contexts and their tabs are disposed together. Scratch-browser launchers use a
machine-wide ownership registry, refuse instance six, and a watchdog kills only their
unique profile if the launcher crashes. Attached user browsers are never owned or killed.

## Batch network reads

```python
out = fetch_all(urls, concurrency=5)   # in-page, uses the session's cookies
print(out.observed)   # {"attempted": 30, "succeeded": 27, "failed": 3}
```

Bounded and counted: it cannot silently return 163 of 300. 429/5xx retry in-page; a 404 is
`http_error` and is not retried.

## Debugging

```bash
BH_JOURNAL=/tmp/run.jsonl bh <<'PY' ... PY
BH_CDP_TRACE=1 BH_JOURNAL=/tmp/run.jsonl bh <<'PY' ... PY
bh trace /tmp/run.jsonl            # span tree with CDP round-trip counts per call
bh trace /tmp/run.jsonl --tail 20
bh --doctor                        # why the browser can or cannot be reached
bh skills which https://acme.jobs.personio.de/job/1
```

For failure-focused runs, call `start_diagnostics()` before navigation and
`diagnostics()` at the terminal state. The bounded result contains HTTP failure shape,
console/exception hashes, target/frame lifecycle, selected renderer metrics, resource
counts and event-loop delay. It excludes URLs, headers, bodies, console text and form
values. `bh stats` reports recovered CDP failures separately from failed helpers.

`bh trace` puts `cdp=N` on every line. If a call shows a large count, it is doing work
per-item that could be batched.

`BH_CDP_TRACE=1` also records one sanitized event per protocol round trip: method, parent
helper, latency, request/response byte counts, parameter keys, result keys, and outcome.
It never records values, which may contain cookies, form answers, uploaded paths,
JavaScript source, or screenshot bytes.

## Gotchas, each paid for

- **A bare async IIFE returns `{}`** under replMode. Write top-level `await (async () => …)()`.
- **`goto` is not "no exception"** — it raises on `chrome-error://`, so a 404 can never be
  reported as a page title.
- **Do not trigger `alert`/`confirm` casually.** A click that opens one blocks the renderer
  mid-dispatch; the harness handles it (reports `dialog` in the delta, dismisses it), but
  raw `cdp("Input...")` calls of your own will hang.
- **A click's DOM delta is `None` after a navigation** — the counter belongs to the old
  document, and a number spanning two documents would be a lie.
- **Harness internals live in an isolated world**, so `window.__bh` is invisible to the
  page. The main-world dry-run boundary uses a symbol rather than a named global. Your
  `js()` runs in the *main* world, where the page's globals are.
- **`navigator.webdriver` is `true`** whenever remote debugging is on. That is Chrome, not
  this harness, and nothing here can hide it.
