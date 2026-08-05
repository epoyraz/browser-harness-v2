"""`fetch_all` — the batching surface (DESIGN.md D0, TODO 22).

In-page, so every request rides the page's own cookies and origin — the pattern that
turned a 17-minute agent walk into 15.6 s of fan-out. Bounded, because unbounded was the
original sin: an unbounded fan-out once returned **163 of ~300 results with no error
raised**. Rule 4 of the outcome contract exists because of that run, and this function is
its primary enforcement site: attempted / succeeded / failed, always, and a missing slot
is a *counted failure*, never a silent gap.
"""
from __future__ import annotations

import json

from harness.core.outcome import Class, Outcome, Tally, fail, ok
from harness.ops.page import Tab

#: Substitution, not f-string/%: the JS is full of braces and percent signs.
#: Top-level `await`, NOT a bare async IIFE: under replMode a bare async IIFE's resolved
#: value serialises to {} (measured — awaitPromise is effectively ignored there), while
#: an awaited expression returns the real value.
_FETCH_JS = """await (async () => {
  const urls = __URLS__, conc = __CONC__, retries = __RETRIES__;
  const maxBody = __MAXBODY__, perMs = __PER_MS__;
  const out = new Array(urls.length).fill(null);
  let next = 0;
  async function one(u) {
    for (let a = 0; a <= retries; a++) {
      try {
        const r = await fetch(u, {credentials: 'include', signal: AbortSignal.timeout(perMs)});
        if ((r.status === 429 || r.status >= 500) && a < retries) {
          await new Promise(res => setTimeout(res, 300 * (a + 1)));
          continue;
        }
        const text = await r.text();
        return {url: u, ok: r.ok, status: r.status, body: text.slice(0, maxBody),
                truncated: text.length > maxBody, retries: a};
      } catch (e) {
        if (a < retries) {
          await new Promise(res => setTimeout(res, 300 * (a + 1)));
          continue;
        }
        return {url: u, ok: false, status: 0, error: String(e).slice(0, 200), retries: a};
      }
    }
  }
  const workers = Array.from({length: Math.min(conc, urls.length)}, async () => {
    while (next < urls.length) { const i = next++; out[i] = await one(urls[i]); }
  });
  await Promise.all(workers);
  return out;
})()"""


def fetch_all(tab: Tab, urls: list[str], *, concurrency: int = 5, retries: int = 2,
              max_body: int = 100_000, per_request: float = 15.0,
              timeout: float = 120.0) -> Outcome:
    """Fetch every URL from inside the page. Returns rule 4's outcome: OK only when all
    succeeded, PARTIAL otherwise — with the successes still in `value` and every failure
    typed in `failures`. 429/5xx are retried in-page with backoff; a 404 is `HTTP_ERROR`
    and is *not* retried (it will 404 again)."""
    if not urls:
        return ok([], attempted=0, succeeded=0, failed=0)
    src = (_FETCH_JS
           .replace("__URLS__", json.dumps(list(urls)))
           .replace("__CONC__", str(int(concurrency)))
           .replace("__RETRIES__", str(int(retries)))
           .replace("__MAXBODY__", str(int(max_body)))
           .replace("__PER_MS__", str(int(per_request * 1000))))
    with tab.journal.call("fetch_all", n=len(urls), concurrency=concurrency):
        results = tab.js(src, timeout=timeout) or []
    tally = Tally()
    for i, url in enumerate(urls):
        r = results[i] if i < len(results) else None
        if not isinstance(r, dict):
            # the silent gap, counted: a slot the pool never filled is a failure
            tally.record(fail(Class.JS_EXCEPTION, "no result recorded for this url",
                              url=url))
        elif r.get("ok"):
            tally.record(ok(r))
        else:
            tally.record(fail(Class.HTTP_ERROR,
                              r.get("error") or f"HTTP {r.get('status')}",
                              url=url, status=r.get("status"),
                              retries=r.get("retries")))
    return tally.outcome(concurrency=concurrency)
