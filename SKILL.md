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
`element_gone`, `no_option_match`, `needs_interaction`, `not_a_form`, `http_error`,
`timeout`, `target_gone`, `session_stale`, `partial`.

## Read the page

```python
snapshot()      # interactive elements: ref, tag, name, x, y, w, h  (~450 in ~3 ms)
page_text()     # rendered innerText, truncated
form_schema()   # {verdict, fields, files} — labels, required, options, refs
capture_screenshot("shot.jpeg", max_dim=800)
```

`snapshot()` gives every element a **ref** and exact viewport-CSS coordinates. Prefer refs
over writing your own selectors. Screenshots are a last resort: a schema is ~175 tokens
where a screenshot of the same form is ~3,200.

## Act

```python
click_ref(ref) / click_at(x, y)      # coordinate clicks pass through iframes + shadow DOM
press_key("Enter") / scroll(600)
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
])
if not out.ok:
    for f in out.failures:
        print(f.cls.value, f.observed)
```

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
