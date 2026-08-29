"""Record as one applicant, replay as another: the recording must carry the site, never the person."""
import json
from pathlib import Path

import pytest

from applications import ontology
from applications.replay import (
    RETIRE_AFTER_FAILURES,
    RecordingStore,
    match_fields_by_label,
    plan_for_recording,
    record_from_result,
)
from harness.ops.profile import ApplicantProfile

PERSONAS = Path(__file__).resolve().parents[1] / "fixtures" / "personas"


def persona(name: str) -> dict:
    return json.loads((PERSONAS / f"{name}.json").read_text(encoding="utf-8"))


def configure(name: str) -> dict:
    profile = persona(name)
    ontology.configure(ApplicantProfile.from_mapping(profile, source=name), profile, "cv.pdf")
    return profile


def _schema_fields():
    return [
        {"ref": "e1", "kind": "text", "label": "Vorname *", "name": "firstName", "selector": "#firstName", "required": True},
        {"ref": "e2", "kind": "text", "label": "Nachname *", "name": "lastName", "selector": "#lastName", "required": True},
        {"ref": "e3", "kind": "email", "label": "E-Mail *", "name": "email", "selector": "#email", "required": True},
        {"ref": "e4", "kind": "tel", "label": "Telefon", "name": "phone", "selector": "#phone", "required": False},
        {"ref": "e5", "kind": "text", "label": "Ort", "name": "city", "selector": "#city", "required": False},
    ]


def discovered_as(name: str) -> dict:
    """What the collector records after a discovery run by `name` (plan values included)."""
    configure(name)
    schema = {"fields": _schema_fields(), "files": [], "verdict": {"is_application": True}}
    plan, audit = ontology.plan_for(schema, "de")
    assert plan, "the corpus planner should fill a plain German form"
    return {
        "status": "form_processed", "job_id": "Kanton_X_1", "company": "Kanton X",
        "start_url": "https://apply.example.ch/1/1/pub/1/index.html",
        "landed_url": "https://apply.example.ch/1/1/index.html?cid=1", "language": "de",
        "hops": [
            {"hop": 0, "url": "https://apply.example.ch/1/1/pub/1/index.html", "is_application": False,
             "apply_link": "https://apply.example.ch/1/1/index.html?cid=1",
             "apply_link_label": "Jetzt bewerben", "apply_link_selector": "a[name=\"apply\"]",
             "transition": {"kind": "link"}},
            {"hop": 1, "url": "https://apply.example.ch/1/1/index.html?cid=1", "is_application": True},
        ],
        "schema": schema, "plan": plan, "field_audit": audit, "file_inputs": [],
    }


def test_a_recording_carries_the_site_and_none_of_the_applicant():
    max_profile = persona("max_mustermann")
    rec = record_from_result(discovered_as("max_mustermann"))
    assert rec is not None
    text = json.dumps(rec, ensure_ascii=False)
    for value in (max_profile["first_name"], max_profile["last_name"], max_profile["email"],
                  max_profile["phone"], max_profile["city"]):
        assert value not in text, f"applicant value {value!r} leaked into the recording"
    assert [s["label"] for s in rec["steps"]] == ["Jetzt bewerben"]
    assert {f["selector"] for f in rec["fields"]} >= {"#firstName", "#lastName", "#email"}
    assert all("value" not in f for f in rec["fields"])
    assert rec["fingerprint"] and rec["host"] == "apply.example.ch"


def test_a_recording_made_as_max_replays_as_martina():
    rec = record_from_result(discovered_as("max_mustermann"))
    max_profile, martina = persona("max_mustermann"), persona("martina_musterfrau")
    live_refs = {i: f"live{i}" for i in range(len(rec["fields"]))}

    configure("martina_musterfrau")
    plan, _audit = plan_for_recording(rec, live_refs)
    written = {step["ref"]: step.get("value") for step in plan}
    assert written, "the planner wrote nothing for Martina"
    values = " ".join(str(v) for v in written.values())
    assert martina["first_name"] in values and martina["last_name"] in values
    assert martina["email"] in values
    for leaked in (max_profile["first_name"], max_profile["last_name"], max_profile["email"],
                   max_profile["phone"], max_profile["city"]):
        assert leaked not in values, f"Max's {leaked!r} replayed for Martina"

    configure("max_mustermann")
    plan_max, _ = plan_for_recording(rec, live_refs)
    values_max = " ".join(str(s.get("value")) for s in plan_max)
    assert max_profile["first_name"] in values_max and martina["first_name"] not in values_max


def test_the_two_personas_differ_in_every_identifying_value():
    a, b = persona("max_mustermann"), persona("martina_musterfrau")
    same = [k for k in ("first_name", "last_name", "full_name", "email", "phone", "street", "postal_code",
                        "city", "birth_date_iso", "birth_date_local", "current_company", "current_title",
                        "education", "summary") if a[k] == b[k]]
    assert not same, f"persona values must differ so a mix-up is visible: {same}"


def test_fields_heal_by_label_when_selectors_drift():
    recorded = [{"label": "Vorname *", "kind": "text", "selector": "#old-1"},
                {"label": "E-Mail *", "kind": "email", "selector": "#old-2"},
                {"label": "Ort", "kind": "text", "selector": "#old-3"}]
    live = [{"ref": "n1", "label": "Vorname", "kind": "text"},
            {"ref": "n2", "label": "E-Mail", "kind": "text"},      # became a plain text box
            {"ref": "n9", "label": "Land", "kind": "select"}]
    healed = match_fields_by_label(recorded, live)
    assert healed == {0: "n1", 1: "n2"}          # Ort is genuinely gone


def test_a_recording_is_retired_after_consecutive_failures_and_revived_by_a_re_record(tmp_path):
    store = RecordingStore(tmp_path)
    rec = record_from_result(discovered_as("max_mustermann"))
    store.add(rec)
    assert store.candidates("apply.example.ch") == [rec]
    for _ in range(RETIRE_AFTER_FAILURES):
        store.note(rec, ok=False)
    assert store.candidates("apply.example.ch") == []
    store.add(record_from_result(discovered_as("martina_musterfrau")))   # same form, re-recorded
    fresh = store.candidates("apply.example.ch")
    assert len(fresh) == 1 and fresh[0]["stats"]["consecutive_failures"] == 0
    # persisted as a list per host, and re-loadable
    assert RecordingStore(tmp_path).count() == 1


def test_preflight_names_the_required_fields_this_applicant_cannot_answer():
    """Per (recording, applicant), before any navigation."""
    from applications.replay import missing_required
    rec = record_from_result(discovered_as("max_mustermann"))
    # The site adds a required tailored question no profile can answer.
    rec["fields"].append({"selector": "#q1", "label": "In one word, what does querySelector return? *",
                          "kind": "text", "name": "q1", "required": True, "semantic": None})
    martina = persona("martina_musterfrau")
    max_p = persona("max_mustermann")
    gaps_martina = missing_required(rec, "de", applicant=ApplicantProfile.from_mapping(martina, source="m"),
                                    profile=martina)
    gaps_max = missing_required(rec, "de", applicant=ApplicantProfile.from_mapping(max_p, source="x"),
                                profile=max_p)
    assert any("querySelector" in str(g.get("label")) for g in gaps_martina)
    assert any("querySelector" in str(g.get("label")) for g in gaps_max)
    # the answerable fields are not reported for either persona
    for gaps in (gaps_martina, gaps_max):
        assert not any(str(g.get("label")).startswith(("Vorname", "Nachname", "E-Mail")) for g in gaps)
    # asking about a person leaves the configured applicant untouched
    assert ontology.PROFILE.get("first_name") == "Max"


def test_martina_is_never_offered_a_male_salutation_or_a_masters_degree():
    from applications.ontology import option_candidates, profile_value
    martina = persona("martina_musterfrau")
    configure("martina_musterfrau")
    item = profile_value("gender_or_salutation", "de")
    labels = option_candidates("gender_or_salutation", item.value, "de", item)
    assert labels[0] == "Frau"
    assert not any(label in labels for label in ("Herr", "Mr", "Male", "männlich", "Mann"))
    edu = profile_value("education", "de")
    rungs = option_candidates("education", edu.value, "de", edu)
    assert rungs[0] == martina["education"] and "Bachelor" in rungs
    assert not any("Master" in r for r in rungs)
    configure("max_mustermann")
    item = profile_value("gender_or_salutation", "en")
    assert option_candidates("gender_or_salutation", item.value, "en", item)[0] == "Mr"


@pytest.fixture(autouse=True)
def _restore_ontology():
    """Put back whoever was configured before — other test modules rely on the applicant
    the package configured at import, so 'empty' is not a neutral state."""
    saved = (ontology.APPLICANT, ontology.PROFILE, ontology.CV)
    yield
    ontology.configure(*saved)


def test_real_gaps_ignores_widget_mirrors_and_repeats():
    """A planned <select> and its widget button share a label: not a gap. Ja/Nein pairs: one gap."""
    from applications.replay import real_gaps

    planned = [{"selector": "#country", "label": "Land *", "kind": "select", "semantic": "country"}]
    unplanned = [
        {"selector": "#country-button", "label": "Land *", "kind": "combobox", "semantic": "country"},
        {"selector": "#permit_ja", "label": "Arbeitserlaubnis *", "kind": "select"},
        {"selector": "#permit_nein", "label": "Arbeitserlaubnis *", "kind": "select"},
        {"selector": "#q9", "label": None, "kind": "text"},
    ]
    gaps = real_gaps(unplanned, planned)
    assert [g["selector"] for g in gaps] == ["#permit_ja", "#q9"]


def test_unfilled_marks_only_answerable_required_fields_blocking():
    """Optional fields, known gaps and selects without a matching option are not a reason to
    re-discover; a required field the profile answers that did not stick is."""
    from applications.replay import _unfilled

    class Outcome:
        value = [{"ref": "r1", "ok": True},
                 {"ref": "r2", "ok": False, "error": "no_option_match"},
                 {"ref": "r3", "ok": False, "error": "element_gone"},
                 {"ref": "r4", "ok": False, "error": "value did not stick"},
                 {"ref": "r5", "ok": False, "error": "value did not stick"}]

    fields = [{"label": "Vorname *", "required": True}, {"label": "Land *", "required": True},
              {"label": "Kommentar", "required": False}, {"label": "Wie haben Sie uns gefunden? *", "required": True},
              {"label": "E-Mail *", "required": True}]
    refs = {0: "r1", 1: "r2", 2: "r3", 3: "r4", 4: "r5"}
    recording = {"required_unplanned": [{"label": "Wie haben Sie uns gefunden? *"}]}
    rows = _unfilled(Outcome(), refs, fields, recording)
    assert [(r["label"], r["blocking"]) for r in rows] == [
        ("Land *", False), ("Kommentar", False), ("Wie haben Sie uns gefunden? *", False), ("E-Mail *", True)]
