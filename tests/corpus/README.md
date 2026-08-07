# Offline form corpus

`forms/` contains the 23 application pages whose dry runs filled at least four fields.
They are rendered, sanitized form fragments captured from an isolated Chrome profile:
site scripts and live submission URLs are removed, field values are cleared, and a CSP
blocks external network access.

Rebuild the corpus with:

```sh
uv run python tools/capture_form_corpus.py --workers 5
```

Compare the old multi-call preparation path with the candidate composite helper using:

```sh
uv run python tests/bench/form_corpus.py
```

The benchmark exits non-zero for a schema mismatch, more CDP calls, or slower aggregate
runtime. Timing is local and measures harness overhead rather than production networking.
