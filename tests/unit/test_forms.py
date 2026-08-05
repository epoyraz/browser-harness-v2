"""forms plumbing against the fake. The layout-dependent half (proximity labels, the 249
options, furniture) runs in tests/live/forms_check.py against real Chrome — geometry is
exactly what a fake cannot testify to."""
import pytest

from harness.connect.cdp import Connection
from harness.connect.session import SessionRegistry
from harness.core.outcome import Class, NotAForm
from harness.ops.forms import fill_form, form_schema, require_form, set_value
from harness.ops.page import Tab
from tests.fake_browser import FakeBrowser


@pytest.fixture
def tab():
    browser = FakeBrowser("a")
    conn = Connection(browser).start()
    t = Tab(conn, SessionRegistry(conn), "a")
    yield browser, t
    conn.close()


def _evaluates(browser):
    return [c for c in browser.calls if c.get("method") == "Runtime.evaluate"]


# --- fill_form aggregation (rule 4) ------------------------------------------

def test_a_clean_fill_is_ok_and_one_evaluate(tab):
    browser, t = tab
    browser.eval_hook = lambda e: [
        {"ref": "e1", "ok": True, "want": "Enes", "got": "Enes"},
        {"ref": "e2", "ok": True, "want": True, "got": True}]
    before = len(_evaluates(browser))
    out = fill_form(t, [{"ref": "e1", "value": "Enes"}, {"ref": "e2", "value": True}])
    assert out.ok is True and out.observed["attempted"] == 2
    assert len(_evaluates(browser)) - before == 1            # the whole form, one write


def test_one_bad_field_makes_the_whole_fill_partial_with_the_report_kept(tab):
    browser, t = tab
    browser.eval_hook = lambda e: [
        {"ref": "e1", "ok": True, "want": "x", "got": "x"},
        {"ref": "e2", "ok": False, "error": "no_option_match", "want": "Atlantis",
         "candidates": ["Suisse (+41)", "Espagne (+34)"]}]
    out = fill_form(t, [{"ref": "e1", "value": "x"}, {"ref": "e2", "label": "Atlantis"}])
    assert out.ok is False and out.cls is Class.PARTIAL
    assert out.observed["succeeded"] == 1 and out.observed["failed"] == 1
    assert out.failures[0].cls is Class.NO_OPTION_MATCH
    assert "Suisse (+41)" in out.failures[0].observed["candidates"]


def test_a_vanished_ref_is_element_gone_in_the_tally(tab):
    browser, t = tab
    browser.eval_hook = lambda e: [{"ref": "e9", "ok": False, "error": "element_gone"}]
    out = fill_form(t, [{"ref": "e9", "value": "x"}])
    assert out.failures[0].cls is Class.ELEMENT_GONE


def test_a_short_report_is_counted_not_trusted(tab):
    """If the in-page pass reports fewer entries than the plan, the gap is a failure."""
    browser, t = tab
    browser.eval_hook = lambda e: [{"ref": "e1", "ok": True}]
    out = fill_form(t, [{"ref": "e1", "value": "x"}, {"ref": "e2", "value": "y"}])
    assert out.observed == {"attempted": 2, "succeeded": 1, "failed": 1, "fields": 2}


def test_plan_values_are_json_injected_quotes_survive(tab):
    browser, t = tab
    seen = {}
    def hook(expr):
        seen["expr"] = expr
        return [{"ref": "e1", "ok": True}]
    browser.eval_hook = hook
    fill_form(t, [{"ref": "e1", "value": 'O\'Brien "Bobby"'}])
    assert '"O\'Brien \\"Bobby\\""' in seen["expr"]


def test_empty_plan_is_ok_zero(tab):
    _, t = tab
    assert fill_form(t, []).ok is True


# --- set_value (item 25) ------------------------------------------------------

def test_default_set_value_is_one_round_trip(tab):
    browser, t = tab
    browser.eval_hook = lambda e: [{"ref": "e1", "ok": True, "want": "x" * 80,
                                    "got": "x" * 80}]
    before = len(_evaluates(browser))
    out = set_value(t, "e1", "x" * 2000)
    assert out.ok is True
    assert len(_evaluates(browser)) - before == 1            # 2,000 chars, one call


def test_keystroke_mode_is_one_inserttext_for_the_whole_string(tab):
    """The opt-in (D3): real input events, still not per-character. v1 spent 61 round
    trips on a 20-char fill."""
    browser, t = tab
    text = "long text " * 20
    browser.eval_hook = lambda e: (True if "focus" in e else text[:80])
    out = set_value(t, "e1", text, keystrokes=True)
    inserts = [c for c in browser.calls if c.get("method") == "Input.insertText"]
    assert len(inserts) == 1 and inserts[0]["params"]["text"] == text
    assert out.ok is True and out.observed["mode"] == "keystrokes"


def test_keystroke_mode_on_a_vanished_ref_fails_typed(tab):
    browser, t = tab
    browser.eval_hook = lambda e: False
    out = set_value(t, "e9", "x", keystrokes=True)
    assert out.ok is False and out.cls is Class.ELEMENT_GONE


# --- schema / verdict plumbing ------------------------------------------------

def test_form_schema_returns_the_page_report(tab):
    browser, t = tab
    payload = {"verdict": {"is_form": True}, "fields": [{"ref": "e1"}], "files": []}
    browser.eval_hook = lambda e: payload
    assert form_schema(t) == payload


def test_require_form_raises_not_a_form_with_the_verdict(tab):
    with pytest.raises(NotAForm) as e:
        require_form({"verdict": {"is_form": False,
                                  "reason": "fewer than 2 real fields after furniture exclusion",
                                  "fields": 0}})
    assert e.value.cls is Class.NOT_A_FORM
    assert "furniture" in str(e.value)
