"""The Python half of `find` / `extract` / `form_values`.

Their bodies are JavaScript and the fake browser does not run JavaScript, so behaviour
lives in `tests/live/query_check.py`. What is testable here is the part a live check
cannot isolate: what expression gets built, how arguments are clamped before they reach
the page, and which browser answer becomes which typed failure.

The three exist because the benchmark traces showed the agent hand-writing them — 47
`querySelectorAll(...).map(...)` lines across three tasks, plus one Python
`next(e for e in snapshot() if ...)` to find a single control.
"""
import json

import pytest

from harness.core.outcome import ElementGone, ScopeRefused
from tests.unit.conftest import _evaluates


def _sent(browser):
    return [json.loads(json.dumps(c["params"]))["expression"] for c in _evaluates(browser)]


def test_find_sends_its_query_into_the_page_rather_than_filtering_here(tab):
    """The point of the helper is that the page does the sifting.

    A `find` implemented as `[e for e in snapshot() if ...]` would still return one row
    while shipping every element across the boundary first, which is the cost the caller
    was trying to avoid.
    """
    browser, t = tab
    browser.eval_hook = lambda e: []
    t.find("basket", tag="button", limit=3)
    expression = _sent(browser)[-1]
    assert '"text": "basket"' in expression and '"tag": "button"' in expression
    assert '"limit": 3' in expression


def test_find_clamps_a_limit_that_would_return_nothing(tab):
    """`limit=0` is a caller slip, and honouring it returns an empty list that reads
    exactly like "no such element" — the one answer a search must never fake."""
    browser, t = tab
    browser.eval_hook = lambda e: []
    t.find("x", limit=0)
    assert '"limit": 1' in _sent(browser)[-1]


def test_extract_passes_the_selector_and_fields_as_json_not_interpolation(tab):
    """A selector is caller text. Formatting it into the expression would let a quote
    close the string and run whatever followed it in the page's isolated world."""
    browser, t = tab
    browser.eval_hook = lambda e: {"rows": [], "matched": 0, "returned": 0}
    t.extract('a[title="x\'y"]', {"t": "h3", "u": "a@href"})
    expression = _sent(browser)[-1]
    assert json.dumps('a[title="x\'y"]') in expression
    assert json.dumps({"t": "h3", "u": "a@href"}) in expression


def test_a_selector_the_browser_rejects_is_a_typed_refusal(tab):
    """`querySelectorAll` throws on invalid CSS. Returning zero rows instead would be
    indistinguishable from a page that genuinely has none."""
    browser, t = tab
    browser.eval_hook = lambda e: {"error": "bad_selector", "detail": "unbalanced ["}
    with pytest.raises(ScopeRefused) as error:
        t.extract("li.card[")
    assert error.value.observed["browser_error"] == "unbalanced ["
    assert error.value.observed["selector"] == "li.card["


def test_extract_reports_what_it_did_not_return(tab):
    """`matched` against `returned` is how a bounded read admits it was bounded."""
    browser, t = tab
    browser.eval_hook = lambda e: {"rows": [{"ref": "e1"}], "matched": 90,
                                   "returned": 1, "truncated": True}
    got = t.extract("li", limit=1)
    assert got["matched"] == 90 and got["returned"] == 1 and got["truncated"]


def test_form_values_reads_the_whole_form_when_given_no_ref(tab):
    browser, t = tab
    browser.eval_hook = lambda e: {"values": [{"ref": "e1", "value": "Ada"}], "returned": 1}
    assert t.form_values()["values"][0]["value"] == "Ada"
    assert _sent(browser)[-1].rstrip().endswith("(null)")


def test_a_stale_ref_says_to_take_a_fresh_snapshot(tab):
    """Refs die with their document. `{}` would read as "this control is empty"."""
    browser, t = tab
    browser.eval_hook = lambda e: {"error": "unknown_ref"}
    with pytest.raises(ElementGone, match="fresh snapshot"):
        t.form_values("e9")
