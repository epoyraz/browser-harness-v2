---
name: browser-harness-v2
description: "Efficient browser control over CDP via bh: bounded page digests, typed outcomes, batched interaction, parallel research, recording, and diagnostics."
---

# Browser Harness v2

Use `bh` for browser interaction, JavaScript-rendered pages, signed-in state, testing, and
browser-only research. A Python program runs against a persistent local Chrome/Edge daemon;
all helpers below are already in scope. Do not import them from `harness` inside a `bh`
script.

On PowerShell, send a here-string (do not use a Bash heredoc):

```powershell
@'
page = open_page("https://example.com")
print({"title": page["page"]["title"], "text": page["page"]["text"][:1000]})
'@ | bh
```

On Bash/zsh, `bh <<'PY' ... PY` is equivalent.

## Efficiency rules

One `bh` program can navigate, inspect several pages, wait, validate, and print one compact
answer. Prefer that over one shell/model round trip per step. The daemon persists, but each
new script still pays setup and repeats output in model context.

**Long runs are one decision.** A batch that takes minutes still belongs in one `bh`
program: start it, block on it, read its one summary. Never poll a running command.
Measured on 2026-08-07 (`docs/benchmarks/application-decisions-2026-08-07.md`): 37 of 46
model invocations in a real run — 6.9 M input tokens, 80% of the total — only checked
whether `bh` had finished; the same work inside one blocking tool call took 1 invocation
and 0 intermediate calls. If the tool that runs `bh` cannot wait that long, use its
background mode and act on the completion notification, not on elapsed time.
`parallel(progress=...)` prints per-item lines for a human watching stderr; the model
needs only the final summary.

Use the highest-level bounded helper that answers the question:

1. `open_page(url)` for navigation plus text, links, metadata, and challenge detection.
2. `read_page()` to reread the current page without navigating. **Prefer it to
   `page_text()`**: it returns the url, title, links and semantic blocks together, so
   `read_page()` replaces `js('location.href') + js('document.title') + page_text()`, and
   a second read of an unchanged document returns references instead of the same text
   again — `page_text()` re-sends all of it every time.
3. `find(text=...)` to locate the element you mean; `extract(selector, fields)` for
   repeated records. Reach for these **before** writing `querySelectorAll(...).map(...)`.
   When a substring is not enough, `find(pattern=r"(apply|bewerb|postul)", exclude=
   r"(newsletter|privacy)", max_len=60)` takes regular expressions and a length cap —
   a sentence containing the word is rarely the control carrying it.
4. `snapshot()` or `form_schema()` when you need every control rather than one;
   `form_values()` to read a form back after writing it.
5. `see()` only when layout, imagery, overlap, or a visually empty page matters.
6. Raw `js()` / `cdp()` only when no helper exposes the required information.

**After an action, read its `consequence` instead of re-reading the page.** `click_ref`,
`type_chars`, `set_value`, `select_option` and `fill_form` already return the changed
regions and the control's observed state. A `read_page()` that follows one of these is
usually a decision spent re-learning what the action just told you.

Print only fields needed for the answer. Do not print full snapshots, giant option arrays,
or repeated page dumps. `page_text()` defaults to 12,000 characters and supports
`page_text(start=12000)`. `read_page()` returns semantic blocks and a document-bound
`cursor`; continue with `read_page(cursor=page["cursor"])`. Raw `start` offsets remain only
for compatibility and cannot detect a document mutation. Request a larger window explicitly
only when necessary.

Treat printed text as one budget across a batch: `max_chars` is **per page**. For five
pages, do not request 10,000 characters five times. Prefer `open_pages(urls,
total_chars=12000)`, or divide the budget yourself and filter in the script before printing.

For research, use search/results for discovery, then verify the requested fact on the best
primary or official source you can reach. Stop once that source supplies the exact evidence;
trying additional mirrors after the answer is verified spends requests and model context
without increasing reliability.

## Read and navigate

```python
r = open_page(url, max_chars=6000, max_links=20)
p = r["page"]
print({
    "landed": r["landed"], "lifecycle": r["lifecycle"],
    "title": p["title"], "blocks": p["blocks"], "links": p["links"],
    "version": p["document_version"], "cursor": p["cursor"],
    "truncated": p["text_truncated"], "challenge": p["challenge"],
})
```

The first semantic read emits bounded headings, prose, lists, tables, controls, and link
groups. An unchanged second read emits no repeated text: use its `unchanged_refs` and
`content_digest`. After a meaningful DOM change, only changed blocks are emitted and stable
blocks retain their refs. A continuation cursor belongs to one exact document generation;
if the page changes, `read_page(cursor=...)` raises typed `DocumentVersionStale`. Recover
with one cursor-free read, which still returns the unconsumed delta.

`goto(url)` returns `requested`, `landed`, and `lifecycle`. A numeric `usable_after` is an
upper bound; after two exact navigations, session-local timing can reduce it within the
documented 0.5–3.0 second adaptive range. An early result requires no in-flight XHR/fetch/
event stream, a quiet network window, and two equal bounded document probes. Pass
`usable_after=None` when the exact lifecycle event is a hard requirement; it disables both
early paths. A Chrome error page raises `NavigationFailed`.

`open_page()` uses the same landing check to return a page digest, so it replaces the
common `goto + title + page_text + links` sequence with one navigation helper. Its digest
contains bounded visible HTTP(S) links and a `challenge` object.

If a navigation times out, call `read_page()` once before navigating again: a stalled
subresource can suppress Chrome's lifecycle event even though the requested document is
already readable. Do not repeatedly reload the same URL.

If `challenge.detected` is true, or the page visibly asks for a CAPTCHA/human verification,
stop that item and report the challenge. Never solve, click, wait out, or repeatedly reload
a CAPTCHA. Continue with other independent items if the task allows it.

For several independent public pages, use the bounded concurrent helper:

```python
rows = open_pages(urls, workers=5, total_chars=12000, max_links=10)
print([r["value"] for r in rows if r.get("ok")])
```

It shares cache/cookies, preserves input order, counts per-page failures, and budgets printed
text across the batch. Use lower-level `parallel()` for custom work; keep workers around
five and normally use `isolated=False` for public research.

After an official URL returns 404, do not batch-guess nearby slugs. Discover the next URL
from the site's own links, search, or sitemap. Guessed variants multiply requests without
adding evidence.

If many same-origin JSON/API URLs are already known, `fetch_all(urls, concurrency=5)`
performs bounded in-page GETs with browser cookies and counted failures. Do not issue one
`js(fetch(...))` per URL.

## Inspect and act

```python
rows = find("Add to basket")             # the elements whose name says this
hits = find(pattern=r"(apply|bewerb)",   # or a regex, with an exclusion and a cap
            exclude=r"(privacy)", max_len=60)
hits = extract("li.card",                # repeated records, each with a ref to act on
               {"title": "h3", "price": ".price", "url": "a@href"})
elements = snapshot()                    # every control: ref, role/tag/name, viewport box
schema = form_schema()                   # labels, required, options, file refs
values = form_values()                   # what the form holds now; passwords read `[set]`
click_ref("e12")                         # verified delta, not silent success
set_value("e4", "text")                 # one round trip
select_option("e7", "Switzerland")      # native or ARIA combobox
wait_for("#results", state="visible", timeout=15)
```

`click_ref`, `type_chars`, `select_option`, `set_value`, and `fill_form` include a bounded
`consequence`. It carries changed semantic regions and observed control validation under a
hard cap. Navigation, JavaScript dialogs, new targets, delivered input, and verified field/
widget state are explicit effects. A bare or unrelated DOM mutation is
`unverified_mutation`, never success; failed writes and selections likewise remain
unverified.

Prefer refs over hand-written selectors. Use `wait_for()` or `wait_lifecycle()` instead of
`time.sleep()`. `see(path=None, marks=True, max_dim=1400)` returns a screenshot and the
elements indexed on it; escalate to it when structure and observed behavior disagree.

Batch forms into one decision and one write:

```python
schema = form_schema()
by_label = {f["label"]: f for f in schema["fields"]}
out = fill_form([
    {"ref": by_label["First name"]["ref"], "value": "Enes"},
    {"ref": by_label["Country"]["ref"], "label": "Switzerland"},
])
print({"ok": out.ok, "observed": out.observed})
```

`set_value`/form steps support `mode="value"` (one call, default), `"insert"` (trusted
insert), and `"type"` (slow, per-key). Use `type` only for incremental masks/typeaheads.
Selects take labels, not indices. Hidden real controls are marked `hidden_control`; ARIA
widgets are marked `needs_interaction` and should use `select_option`.

The browser is dry-run by default. Submit controls, Enter inside a form, form submission,
mutating fetch/XHR, and beacons are blocked. Filling and file attachment are allowed, but
there is no application-submit override. Respect any stricter user boundary independently.

## Evidence and failures

Helpers return evidence. `click_ref()` reports navigation, URL change, DOM mutations, new
targets, dialogs, and modality. `fill_form()` returns an `Outcome` with `.ok`, `.observed`,
`.value`, and `.failures`. Failures are typed:

```python
from harness.core.outcome import Class, HarnessError
try:
    r = open_page(url)
except HarnessError as e:
    print({"class": e.cls.value, "observed": e.observed})
```

Branch on `e.cls`/`Class`, never error-message wording. Do not automatically retry a
semantic failure. A recovered transport/session failure is already retried by the harness.

Every bare helper result and the complete stdout of one `bh` invocation share the
`BH_OUTPUT_BYTES` ceiling (128 KiB by default). Overflow becomes a compact marker with
`_sha256`, byte count, type, and head/tail previews. The exact typed value is stored in the
private content-addressed cache and is available with `fetch_content(marker["_sha256"])`;
slice or summarize it before printing, because stdout remains capped. Journals record only
mechanical counts, truncation, and digests—not page text, headers, or form values.

`parallel()` is the compositional exception: it returns its full in-process record list so
`summarise(records)` and artifact writers can consume it. Printing that list is still
protected by the invocation-wide stdout ceiling.

## Tabs, recordings, and diagnostics

```python
t = new_tab()                    # about:blank, attached, current
use_tab(t.target_id)
close_tab()

start_diagnostics()
r = open_page(url)
print(diagnostics())             # bounded; no URLs, text, headers, or bodies
```

`parallel()` owns and closes its worker tabs. For work split across fresh `bh` processes,
use `lease_tab()` and resume with `BH_TARGET_LEASE`; do not guess a target from tab order.

Set `BH_RECORD=1` for the backward-compatible `review` recording, or name a profile with
`BH_RECORD=evidence|review|cinematic`. `evidence` keeps one final proof frame at each
high-level action boundary, `review` keeps the established diagnostic action frames, and
`cinematic` also retains nested visual beats. The equivalent explicit API is
`start_recording(profile="evidence")`; `BH_RECORD_PROFILE` selects the profile when
`BH_RECORD=1`.

The recording folder contains `session.jsonl`; inspect it with `bh trace <path>` or
summarize usage with `bh stats`. Frame entries carry their helper span, target, screenshot
wall time, CDP count, and byte count. Suppressed frames carry a mechanical reason, and stats
reports recording overhead separately from browser work. Set `BH_CDP_TRACE=1` for
privacy-safe per-round-trip method/latency/byte counts. Diagnostics and tracing omit request
values, page text, headers, form values, and image payloads.

Useful escape hatches remain available:

```python
print(js("document.title"))
print(js("await fetch('/api/data').then(r => r.json())"))
print(cdp("Browser.getVersion"))
capture_screenshot("shot.jpg", max_dim=900)
```

With `js()`, top-level `await` works; avoid a bare async IIFE. Browser-harness internals run
in an isolated world, while explicit `js()` deliberately runs in the page's main world.
