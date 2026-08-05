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

def test_a_clean_fill_is_ok_and_one_write(tab):
    browser, t = tab
    browser.eval_hook = lambda e: [
        {"ref": "e1", "ok": True, "want": "Enes", "got": "Enes"},
        {"ref": "e2", "ok": True, "want": True, "got": True}]
    before = len(_evaluates(browser))
    out = fill_form(t, [{"ref": "e1", "value": "Enes"}, {"ref": "e2", "value": True}],
                    recheck=0)
    assert out.ok is True and out.observed["attempted"] == 2
    assert len(_evaluates(browser)) - before == 1            # the whole form, one write


def test_round_trips_are_constant_in_the_number_of_fields(tab):
    """The invariant that matters (D15): 2 fields and 40 fields cost the same. v1 paid
    per field — and per character within a field."""
    browser, t = tab
    browser.eval_hook = lambda e: [{"ref": f"e{i}", "ok": True} for i in range(40)]
    counts = []
    for n in (2, 40):
        before = len(_evaluates(browser))
        fill_form(t, [{"ref": f"e{i}", "value": "x"} for i in range(n)])
        counts.append(len(_evaluates(browser)) - before)
    assert counts[0] == counts[1] == 2          # one write + one settle-recheck


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


def test_a_value_the_control_refused_is_value_rejected_not_a_js_exception(tab):
    """The in-page pass throws nothing here — it wrote, and the control rewrote it back.
    Calling that JS_EXCEPTION sent readers hunting a stack trace that never existed, and
    hid the fact that the recovery is a different write mode."""
    browser, t = tab
    browser.eval_hook = lambda e: [
        {"ref": "e1", "ok": False, "want": "00 000 00 00", "got": "+41"}]
    out = fill_form(t, [{"ref": "e1", "value": "00 000 00 00"}], recheck=0)
    assert out.failures[0].cls is Class.VALUE_REJECTED
    assert out.failures[0].observed["got"] == "+41"


def test_a_thrown_step_is_still_a_js_exception(tab):
    """The other side of the split: an actual throw keeps its class."""
    browser, t = tab
    browser.eval_hook = lambda e: [
        {"ref": "e1", "ok": False, "error": "TypeError: el.focus is not a function"}]
    out = fill_form(t, [{"ref": "e1", "value": "x"}], recheck=0)
    assert out.failures[0].cls is Class.JS_EXCEPTION


# --- per-field write modes inside one plan -----------------------------------

def test_a_step_may_carry_its_own_write_mode(tab):
    """Without this a form with one masked field had to abandon fill_form entirely and
    hand-roll set_value per field — the batching win thrown away on exactly the forms
    that need it most."""
    browser, t = tab

    def hook(expr):
        # the batched writer inlines the plan array; the typed tier never does
        if "([{" in expr:
            return [{"ref": "e1", "ok": True, "want": "Test", "got": "Test"}]
        if "el.select" in expr:
            return True                      # the typed tier focuses first
        return "+41 79 000 00 00"            # blur-and-read: what the mask settled on
    browser.eval_hook = hook

    out = fill_form(t, [
        {"ref": "e1", "value": "Test"},
        {"ref": "e2", "value": "+41 79 000 00 00", "mode": "insert"},
    ], recheck=0)
    assert out.ok is True and out.observed == {"attempted": 2, "succeeded": 2,
                                               "failed": 0, "fields": 2}
    assert [r["ref"] for r in out.value] == ["e1", "e2"]      # report keeps plan order
    assert out.value[1]["mode"] == "insert"
    assert any(c.get("method") == "Input.insertText" for c in browser.calls)


def test_a_typed_step_does_not_ride_in_the_batched_write(tab):
    """The batch JS must not receive the typed step, or it would set the value the
    one-shot way first and the mask would already have rejected it."""
    browser, t = tab
    seen = []

    def hook(expr):
        seen.append(expr)
        if "([{" in expr:
            return [{"ref": "e1", "ok": True}]
        if "el.select" in expr:
            return True
        return "x"
    browser.eval_hook = hook
    fill_form(t, [{"ref": "e1", "value": "a"}, {"ref": "e2", "value": "x", "mode": "type"}],
              recheck=0)
    batch = next(x for x in seen if "([{" in x)
    assert '"e1"' in batch and '"e2"' not in batch


def test_an_unknown_mode_is_refused_before_anything_is_written(tab):
    browser, t = tab
    browser.eval_hook = lambda e: []
    with pytest.raises(ValueError, match="mode must be"):
        fill_form(t, [{"ref": "e1", "value": "x", "mode": "telepathy"}])
    # the world bootstrap may have run; no *write* may have — a plan half-applied and
    # then rejected is worse than one refused outright
    assert not [c for c in browser.calls
                if "([{" in (c.get("params") or {}).get("expression", "")
                or str(c.get("method", "")).startswith("Input.")]


# --- set_value (item 25) ------------------------------------------------------

def test_default_set_value_is_one_round_trip(tab):
    browser, t = tab
    browser.eval_hook = lambda e: [{"ref": "e1", "ok": True, "want": "x" * 80,
                                    "got": "x" * 80}]
    before = len(_evaluates(browser))
    out = set_value(t, "e1", "x" * 2000, recheck=0)
    assert out.ok is True
    assert len(_evaluates(browser)) - before == 1            # 2,000 chars, one call


def test_insert_mode_is_one_inserttext_for_the_whole_string(tab):
    """Tier 2 (D3): trusted input events, still not per-character. v1 spent 61 round trips
    on a 20-char fill; this is one command."""
    browser, t = tab
    text = "long text " * 20
    browser.eval_hook = lambda e: (True if "focus" in e else text[:80])
    out = set_value(t, "e1", text, mode="insert")
    inserts = [c for c in browser.calls if c.get("method") == "Input.insertText"]
    assert len(inserts) == 1 and inserts[0]["params"]["text"] == text
    assert out.ok is True and out.observed["mode"] == "insert"


def test_type_mode_dispatches_a_key_pair_per_character(tab):
    """Tier 3, and the reason it must exist: measured on an instrumented page, a one-shot
    write and Input.insertText BOTH opened a keystroke typeahead zero times. Only
    per-character key events did. `insert` does not subsume `type`."""
    browser, t = tab
    browser.eval_hook = lambda e: (True if "focus" in e else "zur")
    out = set_value(t, "e1", "zur", mode="type")
    keys = [c for c in browser.calls if c.get("method") == "Input.dispatchKeyEvent"]
    assert [k["params"]["type"] for k in keys] == ["keyDown", "keyUp"] * 3
    assert [k["params"].get("text") for k in keys if k["params"]["type"] == "keyDown"] \
        == ["z", "u", "r"]
    assert out.ok is True and out.observed["mode"] == "type"


def test_the_bool_spelling_still_selects_insert(tab):
    browser, t = tab
    browser.eval_hook = lambda e: (True if "focus" in e else "x")
    assert set_value(t, "e1", "x", keystrokes=True).observed["mode"] == "insert"


def test_an_unknown_mode_is_rejected_loudly(tab):
    _, t = tab
    with pytest.raises(ValueError):
        set_value(t, "e1", "x", mode="telepathy")


def test_keystroke_mode_on_a_vanished_ref_fails_typed(tab):
    browser, t = tab
    browser.eval_hook = lambda e: False
    out = set_value(t, "e9", "x", mode="insert")
    assert out.ok is False and out.cls is Class.ELEMENT_GONE


# --- the two bugs the 2026-08-05 live run found -------------------------------

def test_an_unsettable_widget_is_needs_interaction_not_a_typeerror(tab):
    """jobs.ch's phone-country control is a DIV[role=combobox]. fill_form treated it as a
    text input and called HTMLInputElement's value setter on it — "Illegal invocation".
    form_schema had already flagged it needs_interaction; the fill has to listen."""
    browser, t = tab
    browser.eval_hook = lambda e: [{"ref": "e1", "ok": False, "error": "needs_interaction",
                                    "tag": "div", "role": "combobox", "want": "+41"}]
    out = fill_form(t, [{"ref": "e1", "value": "+41"}], recheck=0)
    assert out.ok is False
    assert out.failures[0].cls is Class.NEEDS_INTERACTION
    assert out.failures[0].observed["role"] == "combobox"


def test_a_normalising_control_counts_as_filled_after_the_recheck(tab):
    """Measured on jobs.ch: a React phone field rewrites +41791234567 to
    '+41 79 123 45 67'. The immediate el.value === want check is taken too early, so
    without the settle-recheck a successful fill is reported as a failure forever."""
    browser, t = tab
    calls = {"n": 0}

    def hook(expr):
        calls["n"] += 1
        if calls["n"] == 1:                       # the write: value not yet normalised
            return [{"ref": "e1", "ok": False, "want": "+41791234567", "got": ""}]
        return ["+41 79 123 45 67"]               # the settled read-back
    browser.eval_hook = hook
    out = fill_form(t, [{"ref": "e1", "value": "+41791234567"}], recheck=0.01)
    assert out.ok is True
    assert out.value[0]["normalized"] is True and out.value[0]["got"] == "+41 79 123 45 67"


def test_a_genuinely_empty_field_stays_failed_after_the_recheck(tab):
    """The recheck must not launder real failures into successes."""
    browser, t = tab
    calls = {"n": 0}

    def hook(expr):
        calls["n"] += 1
        return ([{"ref": "e1", "ok": False, "want": "Schweiz", "got": ""}] if calls["n"] == 1
                else [""])
    browser.eval_hook = hook
    out = fill_form(t, [{"ref": "e1", "value": "Schweiz"}], recheck=0.01)
    assert out.ok is False and out.observed["failed"] == 1


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
