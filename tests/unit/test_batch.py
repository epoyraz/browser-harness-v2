"""fetch_all plumbing against the fake. The live half runs in tests/live/forms_check.py."""
import pytest

from harness.connect.cdp import Connection
from harness.connect.session import SessionRegistry
from harness.core.outcome import Class
from harness.ops.batch import fetch_all, fetch_observed_json
from harness.ops.page import Tab
from tests.fake_browser import FakeBrowser


@pytest.fixture
def tab():
    browser = FakeBrowser("a")
    conn = Connection(browser).start()
    t = Tab(conn, SessionRegistry(conn), "a")
    yield browser, t
    conn.close()


def _r(url, ok=True, status=200, **kw):
    return {"url": url, "ok": ok, "status": status, **kw}


def test_all_ok_is_ok_with_all_three_counts(tab):
    browser, t = tab
    browser.eval_hook = lambda e: [_r("u1"), _r("u2")]
    out = fetch_all(t, ["u1", "u2"])
    assert out.ok is True
    assert out.observed["attempted"] == 2 and out.observed["failed"] == 0


def test_a_missing_slot_is_a_counted_failure_never_a_silent_gap(tab):
    """The 163-of-300 run: results shorter than the url list must not read as success."""
    browser, t = tab
    browser.eval_hook = lambda e: [_r("u1")]                 # pool "lost" u2 and u3
    out = fetch_all(t, ["u1", "u2", "u3"])
    assert out.ok is False and out.cls is Class.PARTIAL
    assert out.observed == {"attempted": 3, "succeeded": 1, "failed": 2, "concurrency": 5}
    assert [f.observed["url"] for f in []] == []             # and the value keeps the wins
    assert out.value == [_r("u1")]


def test_http_failures_are_typed_with_url_and_status(tab):
    browser, t = tab
    browser.eval_hook = lambda e: [_r("u1"), _r("u2", ok=False, status=404, retries=0)]
    out = fetch_all(t, ["u1", "u2"])
    assert out.ok is False
    failure = out.observed  # counts
    assert failure["succeeded"] == 1 and failure["failed"] == 1


def test_the_urls_and_knobs_are_injected_as_json_not_formatted(tab):
    """Substitution, not f-string: a URL containing braces or quotes must survive."""
    browser, t = tab
    seen = {}
    def hook(expr):
        seen["expr"] = expr
        return []
    browser.eval_hook = hook
    fetch_all(t, ['https://x.test/?q={"a b"}'], concurrency=3, retries=1)
    assert '"https://x.test/?q={\\"a b\\"}"' in seen["expr"]
    assert "conc = 3" in seen["expr"] and "retries = 1" in seen["expr"]


def test_empty_input_is_ok_and_zero_counted(tab):
    _, t = tab
    out = fetch_all(t, [])
    assert out.ok is True and out.observed["attempted"] == 0


def test_the_fetch_js_is_top_level_await_not_a_bare_async_iife():
    """Measured on real Chrome: under replMode a bare async IIFE's resolved value
    serialises to {} — every slot then reads as a silent gap. The template must stay
    an awaited expression."""
    from harness.ops.batch import _FETCH_JS
    assert _FETCH_JS.lstrip().startswith("await (async ")


def _event(t, method, params):
    t._on_event({"method": method, "params": params, "sessionId": t._session_id})


def _document(t, url="https://a.test/list"):
    _event(t, "Page.frameNavigated", {"frame": {"id": "F-main", "url": url}})


def _observe_json(
    t,
    request_id,
    url,
    *,
    method="GET",
    document_url="https://a.test/list",
    headers=None,
    cookies=None,
    request_extra=True,
    response_extra=True,
    response_headers=None,
    resource_type="Fetch",
    frame_id="F-main",
    status=200,
    mime_type="application/json",
    from_service_worker=False,
):
    _event(t, "Network.requestWillBeSent", {
        "requestId": request_id,
        "documentURL": document_url,
        "frameId": frame_id,
        "type": resource_type,
        "request": {"url": url, "method": method, "headers": headers or {}},
    })
    if request_extra:
        _event(t, "Network.requestWillBeSentExtraInfo", {
            "requestId": request_id,
            "headers": headers or {},
            "associatedCookies": cookies or [],
        })
    if response_extra:
        _event(t, "Network.responseReceivedExtraInfo", {
            "requestId": request_id,
            "headers": response_headers or {},
        })
    _event(t, "Network.responseReceived", {
        "requestId": request_id,
        "frameId": frame_id,
        "type": resource_type,
        "hasExtraInfo": response_extra,
        "response": {
            "url": url,
            "status": status,
            "mimeType": mime_type,
            "headers": response_headers or {},
            "fromServiceWorker": from_service_worker,
        },
    })


def _automatic(t, **overrides):
    limits = {
        "max_urls": 10,
        "max_responses": 20,
        "max_total_bytes": 10_000,
        "concurrency": 3,
        "retries": 1,
    }
    limits.update(overrides)
    return fetch_observed_json(t, **limits)


def test_observed_public_json_is_one_anonymous_input_ordered_plan(tab):
    browser, t = tab
    _document(t)
    _observe_json(t, "r2", "https://a.test/api?page=2")
    _observe_json(t, "r1", "https://a.test/api?page=1", method="HEAD")
    seen = {}

    def hook(expression):
        seen["expression"] = expression
        return {
            "results": [
                {"ok": True, "status": 200, "body": "{\"page\":2}",
                 "bytes": 10, "retries": 0, "responses": 1},
                {"ok": True, "status": 200, "body": "",
                 "bytes": 0, "retries": 0, "responses": 1},
            ],
            "response_count": 2,
            "total_bytes": 10,
            "origin_unchanged": True,
        }

    browser.eval_hook = hook
    out = _automatic(t)

    assert out.ok is True
    assert [row["url"] for row in out.value] == [
        "https://a.test/api?page=2", "https://a.test/api?page=1"]
    assert [row["method"] for row in out.value] == ["GET", "HEAD"]
    assert out.observed["attempted"] == 2
    assert out.observed["response_count"] == 2
    assert out.observed["response_permits"] == 2
    assert out.observed["request_count"] == 2
    assert out.observed["total_bytes"] == 10
    expression = seen["expression"]
    assert "credentials: 'omit'" in expression
    assert "referrerPolicy: 'no-referrer'" in expression
    assert "redirect: 'error'" in expression
    assert "maxResponses = 20" in expression and "maxBytes = 10000" in expression
    assert expression.index("api?page=2") < expression.index("api?page=1")


def test_observed_plan_uses_request_order_not_response_completion_order(tab):
    browser, t = tab
    _document(t)
    for request_id, url in (
        ("slow", "https://a.test/api?page=1"),
        ("fast", "https://a.test/api?page=2"),
    ):
        _event(t, "Network.requestWillBeSent", {
            "requestId": request_id,
            "documentURL": "https://a.test/list",
            "frameId": "F-main",
            "type": "Fetch",
            "request": {"url": url, "method": "GET", "headers": {}},
        })
        _event(t, "Network.requestWillBeSentExtraInfo", {
            "requestId": request_id, "headers": {}, "associatedCookies": [],
        })
        _event(t, "Network.responseReceivedExtraInfo", {
            "requestId": request_id, "headers": {},
        })
    for request_id, url in (
        ("fast", "https://a.test/api?page=2"),
        ("slow", "https://a.test/api?page=1"),
    ):
        _event(t, "Network.responseReceived", {
            "requestId": request_id, "frameId": "F-main", "type": "Fetch",
            "hasExtraInfo": True,
            "response": {"url": url, "status": 200, "mimeType": "application/json",
                         "headers": {}},
        })
    seen = {}

    def hook(expression):
        seen["expression"] = expression
        return {
            "results": [
                {"ok": True, "status": 200, "body": "{}", "bytes": 2,
                 "retries": 0, "responses": 1},
                {"ok": True, "status": 200, "body": "{}", "bytes": 2,
                 "retries": 0, "responses": 1},
            ],
            "response_count": 2, "total_bytes": 4, "origin_unchanged": True,
        }

    browser.eval_hook = hook
    out = _automatic(t)

    assert [row["url"] for row in out.value] == [
        "https://a.test/api?page=1", "https://a.test/api?page=2"]
    assert seen["expression"].index("api?page=1") < seen["expression"].index("api?page=2")


def test_observed_plan_omits_cross_origin_mutating_authenticated_and_ambiguous(tab):
    browser, t = tab
    _document(t)
    _observe_json(t, "cross", "https://other.test/api")
    _observe_json(t, "post", "https://a.test/write", method="POST")
    _observe_json(t, "cookie", "https://a.test/private", cookies=[{
        "cookie": {"name": "session"}, "blockedReasons": [],
    }])
    _observe_json(t, "missing-extra", "https://a.test/ambiguous", request_extra=False)
    _observe_json(t, "token", "https://a.test/api?access_token=secret")
    _observe_json(t, "worker", "https://a.test/sw", from_service_worker=True)
    _observe_json(t, "safe", "https://a.test/public?page=2")
    seen = {}

    def hook(expression):
        seen["expression"] = expression
        return {
            "results": [{"ok": True, "status": 200, "body": "{}", "bytes": 2,
                         "retries": 0, "responses": 1}],
            "response_count": 1, "total_bytes": 2, "origin_unchanged": True,
        }

    browser.eval_hook = hook
    out = _automatic(t)

    assert out.ok is True and out.observed["attempted"] == 1
    assert out.value[0]["url"] == "https://a.test/public?page=2"
    assert "other.test" not in seen["expression"]
    assert "a.test/write" not in seen["expression"]
    assert "access_token" not in seen["expression"]
    refusals = out.observed["selection"]["refusals"]
    assert {row["class"] for row in refusals} == {
        Class.SCOPE_REFUSED.value, Class.SIDE_EFFECT_REFUSED.value}
    assert {row["reason"] for row in refusals} >= {
        "cross_origin_or_unknown_origin",
        "mutating_or_body_bearing_request",
        "authenticated_request_or_response",
        "request_credential_evidence_ambiguous",
        "credential_bearing_url",
        "service_worker_may_change_replay_authority",
    }


def test_ambiguous_endpoint_evidence_returns_typed_browser_fallback_without_fetch(tab):
    browser, t = tab
    _document(t)
    _observe_json(t, "private", "https://a.test/private", headers={
        "Authorization": "Bearer secret",
    })
    browser.eval_hook = lambda expression: pytest.fail("fallback must not issue a fetch plan")

    out = _automatic(t)

    assert out.ok is False and out.cls is Class.SCOPE_REFUSED
    assert out.observed["attempted"] == 0
    assert out.observed["fallback"] == "browser_interaction"
    assert out.failures[0].cls is Class.SCOPE_REFUSED
    assert out.observed["selection"]["refusals"][0]["url_sha256"]


def test_full_navigation_discards_previous_document_endpoint_evidence(tab):
    browser, t = tab
    _document(t)
    _observe_json(t, "old", "https://a.test/old")
    _document(t, "https://a.test/new-page")
    browser.eval_hook = lambda expression: pytest.fail("stale endpoint must not be replayed")

    out = _automatic(t)

    assert out.cls is Class.SCOPE_REFUSED
    assert out.observed["selection"]["observations"] == 0
    assert out.observed["selection"]["document_generation"] == 2


def test_same_document_navigation_requires_fresh_endpoint_evidence(tab):
    browser, t = tab
    _document(t)
    _observe_json(t, "old", "https://a.test/old-route-data")
    _event(t, "Page.navigatedWithinDocument", {
        "frameId": "F-main", "url": "https://a.test/list?page=2",
    })
    browser.eval_hook = lambda expression: pytest.fail("old route endpoint must not replay")

    out = _automatic(t)

    assert out.cls is Class.SCOPE_REFUSED
    assert out.observed["selection"]["observations"] == 0
    assert out.observed["selection"]["document_generation"] == 2


def test_url_ceiling_and_fetch_failures_keep_complete_ordered_accounting(tab):
    browser, t = tab
    _document(t)
    for index in range(3):
        _observe_json(t, str(index), f"https://a.test/api?page={index}")

    browser.eval_hook = lambda expression: {
        "results": [
            {"ok": False, "errorClass": "resource_limit",
             "error": "response-count ceiling reached", "bytes": 0,
             "retries": 0, "responses": 0},
            {"ok": True, "status": 200, "body": "{}", "bytes": 2,
             "retries": 0, "responses": 1},
        ],
        "response_count": 1,
        "total_bytes": 2,
        "origin_unchanged": True,
    }
    out = _automatic(t, max_urls=2, max_responses=1)

    assert out.ok is False and out.cls is Class.PARTIAL
    assert out.observed["attempted"] == 2
    assert out.observed["succeeded"] == 1 and out.observed["failed"] == 1
    assert [row["url"] for row in out.value] == [
        "https://a.test/api?page=0", "https://a.test/api?page=1"]
    assert out.value[0]["class"] == Class.RESOURCE_LIMIT.value
    assert out.value[1]["class"] == Class.OK.value
    assert out.failures[0].cls is Class.RESOURCE_LIMIT
    refusal = out.observed["selection"]["refusals"][0]
    assert refusal["class"] == Class.RESOURCE_LIMIT.value
    assert refusal["reason"] == "url_count_ceiling_reached"


def test_response_permits_requests_and_actual_responses_are_distinct(tab):
    browser, t = tab
    _document(t)
    _observe_json(t, "timeout", "https://a.test/slow")
    browser.eval_hook = lambda expression: {
        "results": [{
            "ok": False, "errorClass": "http_error", "error": "TimeoutError",
            "bytes": 0, "retries": 0, "responses": 0, "requests": 1,
        }],
        "response_count": 0, "response_permits": 1, "request_count": 1,
        "total_bytes": 0, "origin_unchanged": True,
    }

    out = _automatic(t)

    assert out.ok is False
    assert out.observed["response_count"] == 0
    assert out.observed["response_permits"] == 1
    assert out.observed["request_count"] == 1
    assert out.value[0]["responses"] == 0 and out.value[0]["requests"] == 1


@pytest.mark.parametrize("name,value", [
    ("max_urls", 0),
    ("max_responses", 0),
    ("max_total_bytes", 0),
    ("concurrency", 0),
    ("retries", -1),
])
def test_automatic_endpoint_ceiling_validation_is_fail_closed(tab, name, value):
    _, t = tab
    kwargs = {
        "max_urls": 10,
        "max_responses": 20,
        "max_total_bytes": 10_000,
        "concurrency": 3,
        "retries": 1,
    }
    kwargs[name] = value
    with pytest.raises(ValueError, match=name):
        fetch_observed_json(t, **kwargs)
