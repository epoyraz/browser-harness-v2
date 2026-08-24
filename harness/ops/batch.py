"""`fetch_all` — the batching surface (DESIGN.md D0, TODO 22).

In-page, so every request rides the page's own cookies and origin — the pattern that
turned a 17-minute agent walk into 15.6 s of fan-out. Bounded, because unbounded was the
original sin: an unbounded fan-out once returned **163 of ~300 results with no error
raised**. Rule 4 of the outcome contract exists because of that run, and this function is
its primary enforcement site: attempted / succeeded / failed, always, and a missing slot
is a *counted failure*, never a silent gap.
"""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from harness.core.outcome import Class, Outcome, Tally, fail, ok
from harness.ops.page import Tab

# The caller supplies the five operational ceilings for automatic endpoint replay. These
# second-order caps stop a typo from turning an explicit-but-absurd value into an unbounded
# browser workload.
MAX_OBSERVED_URLS = 256
MAX_OBSERVED_RESPONSES = 1_024
MAX_OBSERVED_BYTES = 8 * 1024 * 1024
MAX_OBSERVED_CONCURRENCY = 16
MAX_OBSERVED_RETRIES = 4

_CREDENTIAL_QUERY_NAMES = frozenset({
    "access_token", "api_key", "apikey", "auth", "authorization", "bearer",
    "client_secret", "code", "credential", "id_token", "jwt", "key", "password",
    "secret", "session", "session_id", "sid", "sig", "signature", "token",
    "key_pair_id", "x_amz_credential", "x_amz_security_token", "x_amz_signature",
    "x_goog_credential", "x_goog_signature",
})

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

# Unlike the legacy explicit fetch_all surface, this plan is intentionally anonymous:
# observed public evidence never grants authority to send cookies, bearer headers, a
# referrer, or a redirect to another origin. Streaming enforces one decoded-body byte
# budget across all workers; the response permit is taken before fetch so concurrency can
# never overshoot the response-count ceiling.
_OBSERVED_FETCH_JS = """await (async () => {
  const plan = __PLAN__, expectedOrigin = __ORIGIN__;
  const conc = __CONC__, retries = __RETRIES__, maxResponses = __MAX_RESPONSES__;
  const maxBytes = __MAX_BYTES__, perMs = __PER_MS__;
  const out = new Array(plan.length).fill(null);
  let next = 0, responsePermits = 0, responseCount = 0;
  let requestCount = 0, totalBytes = 0;

  const refused = (item, index, cls, error, extra = {}) => ({
    index, url: item.url, method: item.method, ok: false,
    errorClass: cls, error, retries: 0, responses: 0, requests: 0, bytes: 0, ...extra
  });
  let originOK = false;
  try { originOK = location.origin === expectedOrigin; } catch {}
  if (!originOK) {
    return {results: plan.map((item, index) => refused(
      item, index, 'scope_refused', 'document origin changed before replay')),
      response_count: 0, response_permits: 0, request_count: 0,
      total_bytes: 0, origin_unchanged: false};
  }

  function responsePermit() {
    if (responsePermits >= maxResponses) return false;
    responsePermits++;
    return true;
  }

  async function readBody(response) {
    if (!response.body || typeof response.body.getReader !== 'function')
      return {ok: false, error: 'streaming response body unavailable'};
    const reader = response.body.getReader(), decoder = new TextDecoder();
    let body = '', bytes = 0;
    try {
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        const value = chunk.value || new Uint8Array();
        const available = Math.max(0, maxBytes - totalBytes);
        const take = Math.min(value.byteLength, available);
        if (take) {
          totalBytes += take; bytes += take;
          body += decoder.decode(value.subarray(0, take), {stream: true});
        }
        if (take < value.byteLength) {
          try { await reader.cancel(); } catch {}
          return {ok: false, limit: true, body, bytes};
        }
      }
      body += decoder.decode();
      return {ok: true, body, bytes};
    } catch (error) {
      try { await reader.cancel(); } catch {}
      return {ok: false, error: String(error).slice(0, 200), body, bytes};
    }
  }

  async function one(item, index) {
    let responses = 0, requests = 0;
    let parsed;
    try { parsed = new URL(item.url); } catch {
      return refused(item, index, 'scope_refused', 'observed URL is no longer valid');
    }
    if (parsed.origin !== expectedOrigin || !['GET', 'HEAD'].includes(item.method))
      return refused(item, index, item.method === 'GET' || item.method === 'HEAD'
        ? 'scope_refused' : 'side_effect_refused', 'plan failed its browser-side guard');

    for (let attempt = 0; attempt <= retries; attempt++) {
      if (!responsePermit())
        return refused(item, index, 'resource_limit', 'response-count ceiling reached',
          {retries: attempt, responses, requests});
      if (item.method !== 'HEAD' && totalBytes >= maxBytes)
        return refused(item, index, 'resource_limit', 'total-byte ceiling reached',
          {retries: attempt, responses, requests});
      try {
        requestCount++; requests++;
        const response = await fetch(item.url, {
          method: item.method, credentials: 'omit', redirect: 'error',
          referrerPolicy: 'no-referrer', signal: AbortSignal.timeout(perMs)
        });
        responseCount++; responses++;
        if ((response.status === 429 || response.status >= 500) && attempt < retries) {
          try { if (response.body) await response.body.cancel(); } catch {}
          await new Promise(resolve => setTimeout(resolve, 300 * (attempt + 1)));
          continue;
        }
        if (!response.ok) {
          try { if (response.body) await response.body.cancel(); } catch {}
          return refused(item, index, 'http_error', `HTTP ${response.status}`,
            {status: response.status, retries: attempt, responses, requests});
        }
        const contentType = String(response.headers.get('content-type') || '')
          .split(';', 1)[0].trim().toLowerCase();
        if (!(contentType === 'application/json' || contentType === 'text/json'
              || contentType.endsWith('+json'))) {
          try { if (response.body) await response.body.cancel(); } catch {}
          return refused(item, index, 'js_exception', 'replayed response is not JSON',
            {status: response.status, retries: attempt, responses, requests});
        }
        if (item.method === 'HEAD' || response.status === 204 || response.status === 205)
          return {index, url: item.url, method: item.method, ok: true,
            status: response.status, body: '', bytes: 0, retries: attempt,
            responses, requests};
        const read = await readBody(response);
        if (read.limit)
          return refused(item, index, 'resource_limit', 'total-byte ceiling reached',
            {status: response.status, body: read.body, bytes: read.bytes,
             retries: attempt, responses, requests, truncated: true});
        if (!read.ok)
          return refused(item, index, 'js_exception', read.error,
            {status: response.status, bytes: read.bytes,
             retries: attempt, responses, requests});
        try { JSON.parse(read.body); } catch {
          return refused(item, index, 'js_exception', 'JSON body could not be parsed',
            {status: response.status, bytes: read.bytes,
             retries: attempt, responses, requests});
        }
        return {index, url: item.url, method: item.method, ok: true,
          status: response.status, body: read.body, bytes: read.bytes,
          retries: attempt, responses, requests};
      } catch (error) {
        if (attempt < retries) {
          await new Promise(resolve => setTimeout(resolve, 300 * (attempt + 1)));
          continue;
        }
        return refused(item, index, 'http_error', String(error).slice(0, 200),
          {retries: attempt, responses, requests});
      }
    }
  }

  const workers = Array.from({length: Math.min(conc, plan.length)}, async () => {
    while (next < plan.length) { const index = next++; out[index] = await one(plan[index], index); }
  });
  await Promise.all(workers);
  return {results: out, response_count: responseCount,
          response_permits: responsePermits, request_count: requestCount,
          total_bytes: totalBytes,
          origin_unchanged: true};
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
        results = tab._world_js(src, timeout=timeout) or []
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


def _ceiling(name: str, value: int, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer between {minimum} and {maximum}")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}, got {value!r}")
    return value


def _origin(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        return None
    if ":" in host:
        host = f"[{host}]"
    default = 80 if scheme == "http" else 443
    return f"{scheme}://{host}" + (f":{port}" if port is not None and port != default else "")


def _credential_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        if parsed.username or parsed.password or parsed.fragment:
            return True
        names = {
            str(name).strip().lower().replace("-", "_")
            for name, _ in parse_qsl(
                parsed.query, keep_blank_values=True, max_num_fields=256)
        }
    except (TypeError, ValueError):
        return True
    return bool(names & _CREDENTIAL_QUERY_NAMES)


def _is_json_mime(value: Any) -> bool:
    mime = str(value or "").split(";", 1)[0].strip().lower()
    return mime in {"application/json", "text/json"} or mime.endswith("+json")


def _refusal(index: int, row: dict[str, Any], cls: Class, reason: str,
             *, occurrences: int = 1) -> dict[str, Any]:
    url = str(row.get("url") or "")
    return {
        "index": index,
        "method": str(row.get("method") or ""),
        "class": cls.value,
        "reason": reason,
        # Credential-bearing or foreign URLs are evidence, not agent-facing content.
        "url_sha256": hashlib.sha256(url.encode()).hexdigest()[:16],
        "occurrences": occurrences,
    }


def _assess_observation(row: dict[str, Any], snapshot: dict[str, Any]) -> tuple[
        Class | None, str]:
    if not _is_json_mime(row.get("mime_type")):
        return None, "not_json"
    if not row.get("request_seen") or not row.get("response_seen"):
        return Class.SCOPE_REFUSED, "incomplete_request_response_evidence"
    method = str(row.get("method") or "").upper()
    if method not in {"GET", "HEAD"} or row.get("has_post_data"):
        return Class.SIDE_EFFECT_REFUSED, "mutating_or_body_bearing_request"
    if row.get("url_truncated") or row.get("document_url_truncated") \
            or row.get("response_url_truncated"):
        return Class.SCOPE_REFUSED, "truncated_url_evidence"
    if row.get("redirected") or str(row.get("response_url") or "") != row.get("url"):
        return Class.SCOPE_REFUSED, "redirect_or_response_url_ambiguous"
    if str(row.get("resource_type") or "") not in {"XHR", "Fetch"}:
        return Class.SCOPE_REFUSED, "not_a_script_json_request"
    main_frame = str(snapshot.get("main_frame") or "")
    if not main_frame or str(row.get("frame_id") or "") != main_frame:
        return Class.SCOPE_REFUSED, "non_main_or_unknown_frame"
    endpoint_origin = _origin(str(row.get("url") or ""))
    observed_document_origin = _origin(str(row.get("document_url") or ""))
    current_document_origin = _origin(str(snapshot.get("document_url") or ""))
    if (endpoint_origin is None or observed_document_origin is None
            or current_document_origin is None
            or endpoint_origin != observed_document_origin
            or endpoint_origin != current_document_origin):
        return Class.SCOPE_REFUSED, "cross_origin_or_unknown_origin"
    if _credential_url(str(row.get("url") or "")):
        return Class.SCOPE_REFUSED, "credential_bearing_url"
    if not row.get("request_extra_seen") or not row.get("request_extra_complete"):
        return Class.SCOPE_REFUSED, "request_credential_evidence_ambiguous"
    if row.get("request_credentials") or row.get("response_auth") \
            or row.get("response_private"):
        return Class.SCOPE_REFUSED, "authenticated_request_or_response"
    if row.get("response_extra_expected") and not row.get("response_extra_seen"):
        return Class.SCOPE_REFUSED, "response_header_evidence_ambiguous"
    if row.get("from_service_worker"):
        return Class.SCOPE_REFUSED, "service_worker_may_change_replay_authority"
    status = int(row.get("status") or 0)
    if status < 200 or status >= 300:
        return Class.HTTP_ERROR, "observed_response_was_not_successful"
    return Class.OK, "eligible"


def _observed_plan(tab: Tab, max_urls: int) -> tuple[
        list[dict[str, str]], dict[str, Any]]:
    snapshot = tab._endpoint_snapshot()
    rows = list(snapshot.get("observations") or [])
    refusals: list[dict[str, Any]] = []
    ignored_non_json = 0
    groups: OrderedDict[tuple[str, str], list[
        tuple[int, dict[str, Any], Class, str]]] = OrderedDict()

    if int(snapshot.get("observations_dropped") or 0):
        refusals.append({
            "index": 0,
            "method": "",
            "class": Class.RESOURCE_LIMIT.value,
            "reason": "observation_history_truncated",
            "url_sha256": "",
            "occurrences": int(snapshot["observations_dropped"]),
        })
    else:
        for index, row in enumerate(rows):
            cls, reason = _assess_observation(row, snapshot)
            if cls is None:
                ignored_non_json += 1
                continue
            key = (str(row.get("method") or "").upper(), str(row.get("url") or ""))
            groups.setdefault(key, []).append((index, row, cls, reason))

    plan: list[dict[str, str]] = []
    duplicate_observations = 0
    for observations in groups.values():
        duplicate_observations += len(observations) - 1
        rejected = next((item for item in observations if item[2] is not Class.OK), None)
        if rejected is not None:
            index, row, cls, reason = rejected
            refusals.append(_refusal(
                index, row, cls, reason, occurrences=len(observations)))
            continue
        index, row, _, _ = observations[0]
        if len(plan) >= max_urls:
            refusals.append(_refusal(
                index, row, Class.RESOURCE_LIMIT, "url_count_ceiling_reached",
                occurrences=len(observations)))
            continue
        plan.append({"url": str(row["url"]), "method": str(row["method"]).upper()})

    selection = {
        "observations": len(rows) + int(snapshot.get("observations_dropped") or 0),
        "json_candidates": sum(len(group) for group in groups.values()),
        "unique_candidates": len(groups),
        "planned": len(plan),
        "refused": len(refusals),
        "ignored_non_json": ignored_non_json,
        "duplicate_observations": duplicate_observations,
        "observation_limit": int(snapshot.get("observation_limit") or 0),
        "document_generation": int(snapshot.get("document_generation") or 0),
        "refusals": refusals,
    }
    selection["document_origin"] = _origin(str(snapshot.get("document_url") or ""))
    return plan, selection


def fetch_observed_json(
    tab: Tab,
    *,
    max_urls: int,
    max_responses: int,
    max_total_bytes: int,
    concurrency: int,
    retries: int,
    per_request: float = 15.0,
    timeout: float = 120.0,
) -> Outcome:
    """Replay exact, observed public JSON reads as one bounded in-page plan.

    All five workload ceilings are required at the call site. Only same-origin main-frame
    XHR/fetch observations with complete no-cookie/no-authorization evidence, a successful
    JSON response, and an exact GET/HEAD URL enter the plan. Replays omit credentials and
    referrers and reject redirects. When evidence cannot support a safe plan, the typed
    result says to fall back to ordinary browser interaction without issuing a request.
    """
    max_urls = _ceiling(
        "max_urls", max_urls, minimum=1, maximum=MAX_OBSERVED_URLS)
    max_responses = _ceiling(
        "max_responses", max_responses, minimum=1, maximum=MAX_OBSERVED_RESPONSES)
    max_total_bytes = _ceiling(
        "max_total_bytes", max_total_bytes, minimum=1, maximum=MAX_OBSERVED_BYTES)
    concurrency = _ceiling(
        "concurrency", concurrency, minimum=1, maximum=MAX_OBSERVED_CONCURRENCY)
    retries = _ceiling(
        "retries", retries, minimum=0, maximum=MAX_OBSERVED_RETRIES)
    if not isinstance(per_request, (int, float)) or isinstance(per_request, bool) \
            or not 0 < float(per_request) <= 60:
        raise ValueError("per_request must be a positive number no greater than 60 seconds")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) \
            or not 0 < float(timeout) <= 300:
        raise ValueError("timeout must be a positive number no greater than 300 seconds")

    plan, selection = _observed_plan(tab, max_urls)
    ceilings = {
        "urls": max_urls,
        "responses": max_responses,
        "total_bytes": max_total_bytes,
        "concurrency": concurrency,
        "retries": retries,
    }
    if not plan:
        refusal_outcomes = [fail(
            Class(item["class"]), item["reason"],
            index=item["index"], method=item["method"],
            url_sha256=item["url_sha256"], occurrences=item["occurrences"])
            for item in selection["refusals"]]
        return Outcome(
            ok=False,
            cls=Class.SCOPE_REFUSED,
            detail="no unambiguous public same-origin JSON endpoint was observed",
            observed={
                "attempted": 0, "succeeded": 0, "failed": 0,
                "selection": selection, "ceilings": ceilings,
                "response_count": 0, "total_bytes": 0,
                "fallback": "browser_interaction",
            },
            value=[],
            failures=refusal_outcomes,
        )

    src = (_OBSERVED_FETCH_JS
           .replace("__PLAN__", json.dumps(plan))
           .replace("__ORIGIN__", json.dumps(selection["document_origin"]))
           .replace("__CONC__", str(concurrency))
           .replace("__RETRIES__", str(retries))
           .replace("__MAX_RESPONSES__", str(max_responses))
           .replace("__MAX_BYTES__", str(max_total_bytes))
           .replace("__PER_MS__", str(int(float(per_request) * 1000))))
    with tab.journal.call(
            "fetch_observed_json", n=len(plan), concurrency=concurrency,
            max_responses=max_responses, max_total_bytes=max_total_bytes):
        raw = tab._world_js(src, timeout=float(timeout)) or {}
    payload = raw if isinstance(raw, dict) else {}
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    ordered: list[dict[str, Any]] = []
    tally = Tally()
    for index, item in enumerate(plan):
        result = results[index] if index < len(results) else None
        if not isinstance(result, dict):
            failure = fail(
                Class.JS_EXCEPTION, "no result recorded for observed endpoint",
                index=index, url=item["url"], method=item["method"])
            tally.record(failure)
            ordered.append({
                "index": index, **item, "ok": False,
                "class": failure.cls.value, "error": failure.detail,
            })
            continue
        row = {**result, "index": index, "url": item["url"], "method": item["method"]}
        if result.get("ok"):
            row["class"] = Class.OK.value
            tally.record(ok(row))
        else:
            try:
                cls = Class(str(result.get("errorClass") or ""))
            except ValueError:
                cls = Class.JS_EXCEPTION
            row["class"] = cls.value
            failure = fail(
                cls, str(result.get("error") or "observed endpoint fetch failed"),
                index=index, url=item["url"], method=item["method"],
                status=result.get("status"), retries=result.get("retries"),
                responses=result.get("responses"), requests=result.get("requests"),
                bytes=result.get("bytes"))
            tally.record(failure)
        ordered.append(row)

    response_count = int(payload.get("response_count") or 0)
    response_permits = int(payload.get("response_permits") or response_count)
    request_count = int(payload.get("request_count") or response_count)
    total_bytes = int(payload.get("total_bytes") or 0)
    outcome = tally.outcome(
        value=ordered,
        selection=selection,
        ceilings=ceilings,
        response_count=response_count,
        response_permits=response_permits,
        request_count=request_count,
        total_bytes=total_bytes,
        origin_unchanged=bool(payload.get("origin_unchanged")),
    )
    if not outcome.observed["succeeded"]:
        outcome.observed["fallback"] = "browser_interaction"
    return outcome
