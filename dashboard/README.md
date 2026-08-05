# Application dashboard

A local UI over the harness for filling job applications in bulk. Exists because the
harness is otherwise invisible: it drives background tabs and closes them, so a run that
worked and a run that did nothing look identical.

```bash
python dashboard/server.py        # -> http://127.0.0.1:8765
```

- **prompt** — free text, e.g. `Software Engineer Zürich last week`. Parsed into title /
  city / recency and echoed back, so a bad parse is visible instead of silent.
- **Get jobs** — searches joblens, then follows each posting through to its *real* apply
  URL. The domain lies: `jobs.coopjobs.ch` and `jobs.uzh.ch` look self-hosted and both
  redirect to an ATS, so the button is followed rather than the hostname trusted.
- **Run** — fills each form, `BH_DASH_PARALLEL` at a time (default 4).
- Rows are red (not started) → amber (running) → green (filled), each with a screenshot.

## It never submits, and never uploads a CV

Attaching a file to an ATS POSTs it **on selection, not on submit** — Lever's "Analyzing
resume…" appears while the form is still untouched. Uploading would therefore create a
candidate record for an application that was never made, so the fill step skips file
inputs entirely. Submit controls are located (and reported) but never clicked.

## Your details

Read from `dashboard/applicant.json` (gitignored) or `BH_APPLICANT_*`, and editable live
in the UI:

```json
{"first": "", "last": "", "email": "", "phone": "", "city": "", "cover": ""}
```

## Environment

| var | meaning |
|---|---|
| `BH_CHROME` | Chrome binary (auto-detected per platform otherwise) |
| `BH_DASH_PORT` | HTTP port, default 8765 |
| `BH_DASH_PARALLEL` | concurrent fills, default 4 |
| `BH_DASH_WORK` | scratch profile + runtime dir, default under the system temp dir |

It runs a **scratch** Chrome profile, never your daily one, so no consent prompt is needed
and your logged-in sessions are untouched.

## Two things learned the hard way

`ensure_chrome()` probes `Input.dispatchMouseEvent`, not just the TCP port. On Windows an
occluded window keeps its debug port open while the compositor stops acknowledging input,
which presents as every site failing at once rather than as a browser problem.

Concurrency defaults to 4, not 10. Ten parallel clients leaked tabs across runs until the
browser died mid-session.
