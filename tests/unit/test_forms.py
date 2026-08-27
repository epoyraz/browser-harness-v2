"""forms plumbing against the fake. The layout-dependent half (proximity labels, the 249
options, furniture) runs in tests/live/forms_check.py against real Chrome — geometry is
exactly what a fake cannot testify to."""
import pytest

from harness.connect.cdp import Connection
from harness.connect.session import SessionRegistry
from harness.core.outcome import Class, NotAForm
from harness.ops import forms
from harness.ops.forms import (
    application_route_candidates,
    fill_form,
    form_schema,
    prepare_document,
    require_form,
    select_option,
    set_value,
)
from harness.ops.page import Tab
from tests.fake_browser import FakeBrowser


@pytest.fixture
def tab():
    browser = FakeBrowser("a")
    conn = Connection(browser).start()
    t = Tab(conn, SessionRegistry(conn), "a")
    yield browser, t
    conn.close()


def test_application_route_candidates_encodes_ashby_capability():
    posting = "https://jobs.ashbyhq.com/acme/ebd97901-59be-4655-ad13-fcfa8ca17987"
    assert application_route_candidates(posting) == [posting + "/application"]
    assert application_route_candidates(posting + "/application") == []
    assert application_route_candidates("https://example.com/acme/123") == []



def combo_hook(*, tag="div", options=None, has_input=False, state=None,
               batch=None, typed_value=None):
    """One hook that answers every probe `select_option` makes, dispatched on JS shape.

    Written once rather than per test: five ad-hoc lambdas each forgot a different probe.
    `click_at` asks for `[location.href, mutations]` and indexes it positionally, so a hook
    that returns a dict there fails with `KeyError: 0` from three frames away — which is
    exactly what happened.
    """
    seen = {"options": 0}

    def hook(expr):
        # Dispatch on the explicit marker: _FILL_JS contains `el.tagName.toLowerCase()`
        # too, so every looser token also matched the batch write and `report` came back
        # as the string "select".
        if "bh-probe:kind" in expr:
            return tag
        if "role=option" in expr:
            seen["options"] += 1
            opts = options(seen["options"]) if callable(options) else (options or [])
            return {"scope": "aria-controls", "options": opts}
        if "location.href" in expr:
            return ["https://a.test/", 0]          # click_at's before/after probe
        if "([{" in expr:                          # fill_form's batched write
            return batch if batch is not None else []
        if "el.select" in expr:
            return True
        if state is not None:
            return state(seen["options"]) if callable(state) else state
        return {"x": 5, "y": 5, "text": "", "value": typed_value,
                "hasInput": has_input, "inputX": 5, "inputY": 5}
    return hook


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


def test_human_readable_fill_smoothly_reveals_each_field_before_its_write(tab):
    browser, t = tab
    events = []

    def hook(expr):
        if "bh-human-reveal" in expr:
            ref = "e1" if 'refs["e1"]' in expr else "e2"
            events.append(("reveal", ref))
            return {"ok": True, "moved": True, "top": 300, "viewport_height": 800}
        if "([{" in expr:
            ref = "e1" if '"ref": "e1"' in expr else "e2"
            events.append(("fill", ref))
            return [{"ref": ref, "ok": True, "want": "x", "got": "x"}]
        return []

    browser.eval_hook = hook
    out = fill_form(t, [{"ref": "e1", "value": "x"},
                        {"ref": "e2", "value": "x"}],
                    recheck=0, human_readable=True, human_pause=0)

    assert out.ok is True
    assert events == [("reveal", "e1"), ("fill", "e1"),
                      ("reveal", "e2"), ("fill", "e2")]
    assert all(entry["presentation"]["moved"] for entry in out.value)


def test_human_readable_fill_rejects_a_negative_pause(tab):
    _, t = tab
    with pytest.raises(ValueError, match="human_pause"):
        fill_form(t, [{"ref": "e1", "value": "x"}],
                  human_readable=True, human_pause=-0.1)


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


def test_ordered_select_candidates_are_serialized_for_exact_in_page_matching(tab):
    browser, t = tab
    seen = {}

    def hook(expr):
        seen["expr"] = expr
        return [{"ref": "e1", "ok": True, "want": "7+ Jahre", "got": "7+ Jahre"}]

    browser.eval_hook = hook
    out = fill_form(t, [{"ref": "e1", "labels": ["8+", "7+ Jahre"]}], recheck=0)
    assert out.ok is True
    assert '"labels": ["8+", "7+ Jahre"]' in seen["expr"]


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
    assert {key: out.observed[key] for key in ("attempted", "succeeded", "failed", "fields")} \
        == {"attempted": 2, "succeeded": 1, "failed": 1, "fields": 2}
    assert out.observed["consequence"]["effect"] == "partial_validation"


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
    # `escalate=False` because this pins the classification, not the recovery: with the
    # ladder on, the reported `got` is the last attempt's rather than the batch's.
    out = fill_form(t, [{"ref": "e1", "value": "00 000 00 00"}], recheck=0, escalate=False)
    assert out.failures[0].cls is Class.VALUE_REJECTED
    assert out.failures[0].observed["got"] == "+41"


def test_a_refusal_that_survives_escalation_keeps_its_class(tab):
    """The ladder changes what was tried, not what the failure is called."""
    browser, t = tab
    browser.eval_hook = lambda e: [
        {"ref": "e1", "ok": False, "want": "00 000 00 00", "got": "+41"}]
    out = fill_form(t, [{"ref": "e1", "value": "00 000 00 00"}], recheck=0)
    assert out.failures[0].cls is Class.VALUE_REJECTED
    assert out.value[0]["escalated_from"] == "value"


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
    assert out.ok is True
    assert {key: out.observed[key] for key in ("attempted", "succeeded", "failed", "fields")} \
        == {"attempted": 2, "succeeded": 2, "failed": 0, "fields": 2}
    assert out.observed["consequence"]["verified"] is True
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


def test_prepare_document_batches_metadata_schema_and_file_refs(tab):
    browser, t = tab
    payload = {"schema": {"verdict": {"is_form": True}, "fields": [], "files": ["cv"]},
               "url": "https://a.test/apply", "title": "Apply", "language": "en",
               "file_inputs": [{"ref": "e1", "name": "cv", "accept": ".pdf"}],
               "apply_link": None}
    browser.eval_hook = lambda expression: payload
    before = len(_evaluates(browser))
    assert prepare_document(t) == payload
    assert len(_evaluates(browser)) - before == 1


def test_prepare_source_has_a_bounded_structured_application_route_tier():
    from harness.ops.forms import _PREPARE_JS

    assert "applicationUrls.slice(0, 12)" in _PREPARE_JS
    assert "visited++ > 5000" in _PREPARE_JS
    assert "const urlShaped" in _PREPARE_JS
    assert "if (!raw || /[<>\"'\\s]/.test(raw)) return" in _PREPARE_JS


def test_require_form_raises_not_a_form_with_the_verdict(tab):
    with pytest.raises(NotAForm) as e:
        require_form({"verdict": {"is_form": False,
                                  "reason": "fewer than 2 real fields after furniture exclusion",
                                  "fields": 0}})
    assert e.value.cls is Class.NOT_A_FORM
    assert "furniture" in str(e.value)


def test_require_form_accepts_generic_and_authentication_forms(tab):
    for classification in ("generic_form", "login_email_password", "login_email_first"):
        schema = {"verdict": {"is_form": True, "classification": classification}}
        assert require_form(schema) is schema


# --- select_option: the combobox dead end, closed -----------------------------

def test_a_native_select_is_delegated_not_rejected(tab):
    """One call handles both kinds, so a caller never has to branch on `kind` first."""
    browser, t = tab
    browser.eval_hook = combo_hook(tag="select",
                                   batch=[{"ref": "e1", "ok": True, "got": "Herr"}])
    out = select_option(t, "e1", "Herr")
    assert out.ok
    assert not [c for c in browser.calls if c.get("method") == "Input.dispatchMouseEvent"]


def test_no_match_returns_candidates_and_never_guesses(tab):
    """Same contract as a native select: 'the first one' is how v1 chose Spain."""
    browser, t = tab
    browser.eval_hook = combo_hook(options=[{"text": "LinkedIn", "x": 10, "y": 40},
                                            {"text": "Referral", "x": 10, "y": 60}])
    out = select_option(t, "e1", "Atlantis", settle=0.01)
    assert out.ok is False and out.cls is Class.NO_OPTION_MATCH
    assert out.observed["candidates"] == ["LinkedIn", "Referral"]


def test_combobox_accepts_ordered_exact_semantic_candidates(tab):
    browser, t = tab
    browser.eval_hook = combo_hook(
        options=[{"text": "7+ Jahre", "x": 10, "y": 40}],
        state=lambda n: {"x": 5, "y": 5, "text": "7+ Jahre" if n else "",
                         "value": "7+ Jahre" if n else "", "hasInput": False})
    out = select_option(t, "e1", ["8+", "7+ Jahre"], settle=0.01)
    assert out.ok and out.value["got"] == "7+ Jahre"


def test_fill_form_routes_declared_interactive_widgets_through_select_option(tab):
    browser, t = tab
    browser.eval_hook = combo_hook(
        options=[{"text": "LinkedIn", "x": 10, "y": 40}],
        state=lambda n: {"x": 5, "y": 5, "text": "LinkedIn" if n else "",
                         "value": "LinkedIn" if n else "", "hasInput": False})
    out = fill_form(t, [{"ref": "e1", "labels": ["Joblens", "LinkedIn"],
                         "interaction": "select"}], recheck=0)
    assert out.ok and out.observed["succeeded"] == 1


def test_fill_form_accepts_native_selects_returning_a_list_report(tab):
    browser, t = tab
    browser.eval_hook = combo_hook(
        tag="select", batch=[{"ref": "e1", "ok": True, "want": "Herr", "got": "Herr"}])
    out = fill_form(t, [{"ref": "e1", "labels": ["Herr", "Mr"],
                         "interaction": "select"}], recheck=0)
    assert out.ok and out.value[0]["got"] == "Herr"


def test_a_failed_select_closes_the_popup(tab):
    """A listbox left open covers the page and swallows the next click, so a failed
    selection must not also break whatever is attempted afterwards."""
    browser, t = tab
    browser.eval_hook = combo_hook(options=[{"text": "A", "x": 1, "y": 2}])
    select_option(t, "e1", "nope", settle=0.01)
    keys = [c for c in browser.calls if c.get("method") == "Input.dispatchKeyEvent"]
    assert any(k["params"].get("key") == "Escape" for k in keys)


def test_an_empty_popup_is_needs_interaction_not_no_match(tab):
    """Different repairs: 'the widget did not open' is not 'your label was wrong'."""
    browser, t = tab
    browser.eval_hook = combo_hook(options=[])
    out = select_option(t, "e1", "x", settle=0.01)
    assert out.ok is False and out.cls is Class.NEEDS_INTERACTION


def test_a_typeahead_is_typed_into_before_options_are_read(tab):
    """A Workday-shaped widget renders NO options until filtered, so reading once and
    giving up would report an empty list for a list that is merely unqueried."""
    browser, t = tab
    browser.eval_hook = combo_hook(
        has_input=True,
        options=lambda n: [] if n == 1 else [{"text": "Schweiz", "x": 10, "y": 40}],
        state=lambda n: {"x": 5, "y": 5, "text": "", "hasInput": True,
                         "inputX": 5, "inputY": 5,
                         "value": "Schweiz" if n > 1 else ""})
    out = select_option(t, "e1", "Schweiz", settle=0.01)
    typed = [c for c in browser.calls if c.get("method") == "Input.dispatchKeyEvent"
             and c["params"].get("type") == "keyDown" and c["params"].get("text")]
    assert out.ok and "".join(k["params"]["text"] for k in typed) == "Schweiz"


def test_a_vanished_ref_is_element_gone(tab):
    browser, t = tab
    browser.eval_hook = lambda e: None
    out = select_option(t, "e9", "x")
    assert out.ok is False and out.cls is Class.ELEMENT_GONE


# --- the harness walks the write ladder it already knows -----------------------

def _escalation_probe(monkeypatch, outcomes):
    """Record which (ref, mode) pairs are retried; answer each from `outcomes`."""
    seen = []

    def fake(tab, ref, value, mode, timeout):
        seen.append((ref, mode))
        result = outcomes.get((ref, mode), {"ok": False})
        return {"ref": ref, "mode": mode, "want": str(value), **result}

    monkeypatch.setattr(forms, "_typed_write", fake)
    return seen


def test_a_refused_write_is_retried_before_it_is_reported(monkeypatch):
    """`_step_class` already said it: an entry with no `error` executed cleanly and had its
    value refused, and "the recovery is a different write mode". Leaving that to the caller
    cost a round trip to notice and another to retry."""
    seen = _escalation_probe(monkeypatch, {("e2", "insert"): {"ok": True, "got": "12345"}})
    plan = [{"ref": "e1", "value": "hello"}, {"ref": "e2", "value": "12345"}]
    merged = [{"ref": "e1", "ok": True}, {"ref": "e2", "ok": False, "want": "12345"}]
    assert forms._escalate_rejected(object(), plan, merged, 5.0) == 1
    assert seen == [("e2", "insert")]                 # stops as soon as one sticks
    assert merged[1]["ok"] is True
    assert merged[1]["mode"] == "insert"
    assert merged[1]["escalated_from"] == "value"


def test_escalation_climbs_to_typing_when_insert_is_refused(monkeypatch):
    seen = _escalation_probe(monkeypatch, {("e1", "type"): {"ok": True, "got": "x"}})
    merged = [{"ref": "e1", "ok": False}]
    assert forms._escalate_rejected(object(), [{"ref": "e1", "value": "x"}], merged, 5.0) == 1
    assert seen == [("e1", "insert"), ("e1", "type")]


@pytest.mark.parametrize("entry", [
    {"ok": False, "error": "element_gone"},
    {"ok": False, "error": "needs_interaction"},
    {"ok": False, "error": "no_option_match"},
])
def test_only_a_refused_value_escalates(monkeypatch, entry):
    """A missing element and an ARIA widget are not write-mode problems; retrying them
    spends round trips to fail the same way."""
    seen = _escalation_probe(monkeypatch, {})
    merged = [{"ref": "e1", **entry}]
    assert forms._escalate_rejected(object(), [{"ref": "e1", "value": "x"}], merged, 5.0) == 0
    assert seen == []


def test_an_explicit_mode_is_the_callers_decision(monkeypatch):
    """Overriding a deliberately chosen tier would make an explicit mode mean less than a
    default."""
    seen = _escalation_probe(monkeypatch, {})
    merged = [{"ref": "e1", "ok": False}]
    forms._escalate_rejected(object(), [{"ref": "e1", "value": "x", "mode": "type"}],
                             merged, 5.0)
    assert seen == []


def test_escalation_stops_when_the_control_leaves(monkeypatch):
    seen = _escalation_probe(monkeypatch,
                             {("e1", "insert"): {"ok": False, "error": "element_gone"}})
    merged = [{"ref": "e1", "ok": False}]
    forms._escalate_rejected(object(), [{"ref": "e1", "value": "x"}], merged, 5.0)
    assert seen == [("e1", "insert")]        # another mode cannot bring it back


def test_a_still_failing_entry_keeps_the_newest_evidence(monkeypatch):
    """A caller reading a failed entry should see what the last attempt observed."""
    _escalation_probe(monkeypatch, {("e1", "type"): {"ok": False, "got": "partial"}})
    merged = [{"ref": "e1", "ok": False, "got": ""}]
    forms._escalate_rejected(object(), [{"ref": "e1", "value": "xyz"}], merged, 5.0)
    assert merged[0]["got"] == "partial" and merged[0]["mode"] == "type"
