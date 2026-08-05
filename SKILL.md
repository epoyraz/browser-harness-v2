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
upload_file(ref, "/path/cv.pdf")      # no OS picker
js("await fetch('/api').then(r=>r.json())")   # replMode: top-level await works
cdp("Target.getTargets")              # raw escape hatch, always available
```

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
s = require_form(form_schema())          # raises NotAForm on a 404 / cookie-banner page
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
- **`needs_interaction`** — an ARIA combobox built from divs. It has no value to set: click
  it and pick from the popup.
- **`hidden_control`** — the real `<select>` behind a widget. Fill this one; the visible
  1×1 decoy accepts writes and submits nothing.
- **`verdict.is_form`** — a page can render fine and be a cookie banner plus a site search.

## Tabs

```python
t = new_tab("https://example.com")     # creates, attaches, makes current
use_tab(t.target_id)                   # switch
targets()                              # list page targets
close_tab()
```

Your current tab is client-local: two scripts running at once cannot steal each other's.

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
bh trace /tmp/run.jsonl            # span tree with CDP round-trip counts per call
bh trace /tmp/run.jsonl --tail 20
bh --doctor                        # why the browser can or cannot be reached
```

`bh trace` puts `cdp=N` on every line. If a call shows a large count, it is doing work
per-item that could be batched.

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
  page. Your `js()` runs in the *main* world, where the page's globals are.
- **`navigator.webdriver` is `true`** whenever remote debugging is on. That is Chrome, not
  this harness, and nothing here can hide it.
