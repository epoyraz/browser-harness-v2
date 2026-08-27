"""fetch_all plumbing against the fake. The live half runs in tests/live/forms_check.py."""
import pytest

from harness.connect.cdp import Connection
from harness.connect.session import SessionRegistry
from harness.core.outcome import Class
from harness.ops.batch import fetch_all
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
