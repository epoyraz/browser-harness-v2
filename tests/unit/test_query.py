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

from harness.core.outcome import ElementGone, RendererUnresponsive, ScopeRefused
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


def _ax_node(backend, role, name, ignored=False, **props):
    return {"backendDOMNodeId": backend, "ignored": ignored,
            "role": {"value": role}, "name": {"value": name},
            "properties": [{"name": k, "value": {"value": v}} for k, v in props.items()]}


def _ax_browser(tab, nodes):
    """Answer the AX tree, and bind every ref request to a predictable name."""
    browser, t = tab
    browser.eval_hook = lambda e: 1  # any world context id
    original = t.cdp

    def fake(method, params=None, **kw):
        if method == "Accessibility.getFullAXTree":
            return {"nodes": nodes}
        if method == "Accessibility.enable":
            return {}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": f"obj-{params['backendNodeId']}"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": "e" + params["objectId"].split("-")[1]}}
        if method == "Runtime.releaseObjectGroup":
            return {}
        return original(method, params, **kw)

    t.cdp = fake
    return t


def test_ax_keeps_a_node_that_is_named_or_focusable_and_drops_the_rest(tab):
    """A denylist of roles would drop a widget whose role we have never seen. Keeping
    what is named or focusable is a property of the node, not a list we maintain."""
    t = _ax_browser(tab, [
        _ax_node(1, "textbox", "Date of birth", focusable=True),
        _ax_node(2, "generic", ""),                      # structural
        _ax_node(3, "StaticText", "Date of birth"),      # the label's own text node
        _ax_node(4, "some-future-widget", "", focusable=True),
        _ax_node(5, "button", "Hidden", ignored=True),
        _ax_node(6, "img", ""),                          # neither named nor focusable
    ])
    names = [(r["role"], r["name"]) for r in t.ax(refs=False)]
    assert names == [("textbox", "Date of birth"), ("some-future-widget", "")]


def test_ax_binds_each_row_into_the_ordinary_ref_registry(tab):
    """The point of binding: `click_ref` and `set_value` must not learn where a ref came
    from — a ref is looked up in about twenty places."""
    t = _ax_browser(tab, [_ax_node(7, "button", "Send", focusable=True)])
    row = t.ax()[0]
    assert row["ref"] == "e7" and row["ax"] == 7


def test_a_node_that_will_not_resolve_costs_its_own_row_and_no_other(tab):
    """It has usually gone since the tree was read. One detached node must not lose the
    caller the rows that are still good."""
    t = _ax_browser(tab, [_ax_node(1, "button", "Fine", focusable=True),
                          _ax_node(2, "button", "Gone", focusable=True)])
    inner = t.cdp

    def flaky(method, params=None, **kw):
        if method == "DOM.resolveNode" and params["backendNodeId"] == 2:
            raise ElementGone("detached")
        return inner(method, params, **kw)

    t.cdp = flaky
    rows = {r["name"]: r["ref"] for r in t.ax()}
    assert rows == {"Fine": "e1", "Gone": None}


def test_ax_reports_only_the_states_the_platform_actually_set(tab):
    """`checked: "false"` on every unchecked box is noise the caller has to filter."""
    t = _ax_browser(tab, [
        _ax_node(1, "checkbox", "Terms", checked="true", invalid="false", focusable=True),
        _ax_node(2, "checkbox", "Ads", checked="false", invalid="false", focusable=True),
    ])
    rows = {r["name"]: r for r in t.ax(refs=False)}
    assert rows["Terms"]["checked"] == "true"
    assert "checked" not in rows["Ads"] and "invalid" not in rows["Terms"]


# --- Memory-Saver-discarded tabs ------------------------------------------------------

def _renderer(tab, answers):
    """Answer Page.getLayoutMetrics from `answers` in order; "hang" means no renderer."""
    _browser, t = tab
    seen = {"activated": 0, "probes": 0}
    original = t.cdp

    def fake(method, params=None, **kw):
        if method == "Page.getLayoutMetrics":
            seen["probes"] += 1
            reply = answers.pop(0) if answers else "ok"
            if reply == "hang":
                raise RendererUnresponsive("Page.getLayoutMetrics did not answer in 3.0s")
            return {}
        return original(method, params, **kw)

    t.cdp = fake
    t._conn.request = lambda method, params=None, **kw: (
        seen.__setitem__("activated", seen["activated"] + 1) or {})
    return t, seen


def test_a_live_renderer_costs_one_probe_and_no_reactivation(tab):
    """The happy path has to be nearly free, or it cannot sit in front of anything."""
    t, seen = _renderer(tab, ["ok"])
    assert t.ensure_renderer() == "responsive"
    assert seen == {"activated": 0, "probes": 1}


def test_a_discarded_tab_is_reactivated_and_reported_as_revived(tab):
    """CDP exposes no `discarded` flag, so the unanswered probe *is* the signal."""
    t, seen = _renderer(tab, ["hang", "ok"])
    assert t.ensure_renderer() == "revived"
    assert seen == {"activated": 1, "probes": 2}


def test_a_tab_that_stays_dead_says_so_rather_than_raising(tab):
    """The caller asked whether it could proceed. "No" is an answer, and `goto` needs it
    to decide between retrying and re-raising the original failure."""
    t, seen = _renderer(tab, ["hang", "hang"])
    assert t.ensure_renderer() == "unrecoverable"
    assert seen["activated"] == 1
