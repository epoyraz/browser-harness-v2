"""The corpus planner's field ontology, pinned against the way it has actually failed.

Every case here was a wrong answer written into a real form on a real run, and all of them
are one mistake: matching a short token as a substring. `"ort"` inside `"passwort"` filled
six password boxes with a city; `"anrede"` inside `"anredetitel"` answered a Dr./Prof.
select with "Herr". The ontology lives in a benchmark tool rather than in the harness, but
it decides what gets typed into an employer's form, so it is worth a fast test rather than
only a corpus replay.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from applications import ontology

# The ontology is a package beside the harness now, so this imports it instead of loading
# a benchmark script. It needs answers to map fields onto, and the corpus profile is the
# set these cases were captured against.
_TOOL = Path(__file__).parents[2] / "tools" / "collect_job_form_telemetry.py"
_SPEC = importlib.util.spec_from_file_location("collect_job_form_telemetry", _TOOL)
_corpus = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_corpus)

rules = ontology


def classify(label, name=None, kind="text", options=None, group_label=None):
    return rules.semantic({"label": label, "name": name, "kind": kind,
                           "options_sample": options, "group_label": group_label})


@pytest.mark.parametrize(("label", "name", "kind"), [
    ("Wähle ein Kennwort: *", "fbclc_pwd", "password"),
    ("Kennwort erneut eingeben: *", "fbclc_pwdConf", "password"),
    ("Passwort", "form_data13", "password"),
])
def test_a_credential_is_never_a_city(label, name, kind):
    """`"ort" in "passwort"`. Six of these were written with "Neuhausen am Rheinfall" on
    2026-08-25, and `fill.ok` reported success for every one."""
    assert classify(label, name, kind) != "city"


@pytest.mark.parametrize(("label", "expected"), [
    # "ort" inside "rapportees"; the field asks for a number and got a town.
    ("Veuillez indiquer vos prétentions salariales annuelles (format : 75000). *",
     "salary_expectation"),
    # "land" inside "Islander" and "Switzerland".
    ("Native Hawaiian or other Pacific Islander", "unclassified"),
    ("Yes, I am based in Switzerland or willing to relocate", "unclassified"),
    # "experience" is a word in half the essay prompts on these forms.
    (("What is your most relevant work experience / project for this role and why is it "
      "relevant?"), "unclassified"),
    # "formation" inside "information".
    ("Additional information", "unclassified"),
])
def test_a_short_token_does_not_match_inside_a_longer_word(label, expected):
    assert classify(label) == expected


def test_naming_linkedin_is_not_asking_for_a_linkedin_url():
    """"link" is a substring of "linkedin", so a question that merely offered LinkedIn as
    one of its example answers counted as evidence that a profile URL was wanted."""
    assert classify("Where did this job description catch your attention "
                    "(LinkedIn, jobs.ch, open source)?") == "referral_source"
    assert classify("LinkedIn URL", "urls[LinkedIn]") == "linkedin_url"


@pytest.mark.parametrize(("label", "name", "options", "expected"), [
    # What a control offers decides it, before what it is called.
    ("Anredetitel", "bewerbung_form[titel]", ["Anredetitel", "Dr.", "Prof."],
     "academic_title"),
    ("Anrede*", "bewerbung_form[gender]", ["Anrede*", "Herr", "Frau"],
     "gender_or_salutation"),
    # Labelled by the proximity fallback from the select beside it; its name is the truth.
    ("Anrede*", "titel-button", None, "academic_title"),
    # Named `title` and offering Frau/Herr — the options win over the name.
    ("Anrede", "title", ["Anrede", "Frau", "Herr"], "gender_or_salutation"),
    ("Anrede*", "LAMAPPL_xxx_Salutation", ["Bitte wählen"], "gender_or_salutation"),
    # A study question that happens to contain the word "title".
    ("What was the study major & minor title? *", "c23b63a5", None, "education"),
])
def test_a_title_and_a_salutation_are_different_questions(label, name, options, expected):
    """`"anrede"` is a substring of `"anredetitel"`, so ten Dr./Prof. selects and the ten
    combobox widgets over them were answered "Herr" until the options were consulted
    first."""
    assert classify(label, name, "select", options) == expected


@pytest.mark.parametrize(("label", "name", "expected"), [
    ("Stammdaten", "gender-button", "gender_or_salutation"),
    ("Schule, Ausbildung, Beruf", "schulabschluss-button", "education"),
])
def test_a_proximity_label_does_not_hide_what_the_name_says(label, name, expected):
    """These read as section headings because the geometric fallback picked up the heading
    above the widget. The control's own name still says what it is, and a model shown only
    the label declined both."""
    assert classify(label, name, "combobox") == expected


@pytest.mark.parametrize("label", [
    "E-Mail*", "E-Mail-Adresse: *", "Courriel", "Email address",
])
def test_the_standard_german_spelling_of_email_is_recognised(label):
    """`norm()` keeps hyphens, so "E-Mail" — the spelling every Swiss form uses — did not
    contain the substring "email"."""
    assert classify(label) in {"email", "email_confirm"}


@pytest.mark.parametrize(("label", "name", "expected"), [
    ("Postal", "resumator-postal-value", "postal_code"),
    ("State/Province", "resumator-state-value", "state_province"),
    ("Province", "province", "state_province"),
])
def test_jazzhr_address_aliases_are_named(label, name, expected):
    """Bare JazzHR labels were optional, so they vanished from a nominally complete run."""
    assert classify(label, name) == expected


def test_jazzhr_optional_address_fields_are_planned_from_the_profile():
    schema = {"fields": [
        {"ref": "state", "label": "State/Province", "name": "resumator-state-value",
         "kind": "text", "required": False},
        {"ref": "postal", "label": "Postal", "name": "resumator-postal-value",
         "kind": "text", "required": False},
    ]}
    plan, audit = rules.plan_for(schema, "en")
    by_ref = {step["ref"]: step["value"] for step in plan}
    assert by_ref == {"state": "Schaffhausen", "postal": "8212"}
    assert [row["status"] for row in audit] == ["planned", "planned"]


def test_a_repeat_field_is_a_mail_confirmation_only_when_it_is_about_mail():
    """"erneut eingeben" alone is how a form asks for a repeated *password*."""
    assert classify("E-Mail-Adresse erneut eingeben: *") == "email_confirm"
    assert classify("Kennwort erneut eingeben: *", "fbclc_pwdConf",
                    "password") != "email_confirm"


# --- 2026-08-28: the required controls the 77-company corpus could not name ------------
# Every label below is one a real form asked as a select, radio or combobox and the
# ontology left `unclassified`, so the form stayed partial. Options are the ones the
# live schema probe read that day.

@pytest.mark.parametrize(("label", "kind", "expected"), [
    ("Country Phone Code*", "select", "phone_country_code"),
    ("Vorwahl", "select", "phone_country_code"),
    ("adesso Schweiz Wunschstandort*", "select", "preferred_location"),
    ("Sprache", "select", "correspondence_language"),
    ("Job gefunden auf *", "select", "referral_source"),
    ("Auf diesem Kanal bin ich auf den Kanton Bern aufmerksam geworden", "select",
     "referral_source"),
    ("Former/Current Givaudan employee*", "select", "former_employee"),
    ("Bist du bereits Visana-Mitarbeiter*in?", "select", "former_employee"),
    ("Eintrag im Straf- und Betreibungsregister *", "select", "criminal_record"),
    ("Gewünschter Beschäftigungsgrad *", "select", "workload_percent"),
    ("Gehst du einer Nebenbeschäftigung nach?", "select", "side_job"),
    ("Are you open to working fully on-site?* (required)", "select", "onsite_ok"),
    ("Schulabschluss *", "select", "education"),
    ("Veuillez renseigner votre niveau d'étude le plus élevé *", "select", "education"),
])
def test_the_required_selects_of_the_2026_08_28_corpus_are_named(label, kind, expected):
    assert classify(label, kind=kind) == expected


def test_a_referral_name_box_is_not_a_former_employee_question():
    """"Hat ein/e Mitarbeiter/in der CSS dir die Stelle empfohlen?" wants a colleague's
    name in a text box; "Mitarbeiter/in" must not turn it into a yes/no fact."""
    assert classify("Hat ein/e Mitarbeiter/in der CSS dir die Stelle empfohlen?") \
        != "former_employee"


def test_a_language_level_question_is_not_a_correspondence_language():
    assert classify("Sprachkenntnisse Deutsch", kind="select") != "correspondence_language"


def test_a_phone_field_still_classifies_as_phone_next_to_a_country_code_select():
    assert classify("Phone number*", kind="text") == "phone"
    assert classify("Phone (Optional) +41", kind="tel") == "phone"


def _field(ref, label, kind, **extra):
    return {"ref": ref, "label": label, "name": label.lower(), "kind": kind,
            "required": True, **extra}


def test_a_decorator_combobox_twin_of_a_native_select_is_not_planned():
    """Select2-style widgets: `form_schema` reports the native `<select>` and, right
    beside it, a `role=combobox` div with the same label and no value property. Planning
    both wrote the select and then failed the twin with `needs_interaction` — 7 of the 12
    non-option write failures on the 77-company corpus (adesso, Axon Lab, AMAG, Vaudoise)."""
    schema = {"fields": [
        _field("e1", "Anrede*", "select", options_sample=["Anrede*", "Herr", "Frau"]),
        _field("e2", "Anrede*", "combobox", needs_interaction=True),
        _field("e3", "Kündigungsfrist *", "select", widget=True,
               options_sample=["---", "keine", "1 Monat", "3 Monate"]),
        _field("e4", "Kündigungsfrist *", "combobox", needs_interaction=True),
    ]}
    plan, audit = rules.plan_for(schema, "de")
    planned = {step["ref"]: step for step in plan}
    assert "e2" not in planned and "e4" not in planned
    assert [a["status"] for a in audit if a["ref"] in ("e2", "e4")] == ["decorator_twin"] * 2
    # The native select is written by label, never through the widget path.
    assert "labels" in planned["e1"] and "interaction" not in planned["e1"]
    assert "labels" in planned["e3"] and "interaction" not in planned["e3"]


def test_a_lone_combobox_still_gets_the_interaction_path():
    schema = {"fields": [_field("e1", "Anrede*", "combobox", needs_interaction=True)]}
    plan, _ = rules.plan_for(schema, "de")
    assert plan and plan[0]["interaction"] == "select" and "labels" in plan[0]


def test_the_number_loses_its_prefix_when_the_country_code_is_supplied_elsewhere():
    """Givaudan: "Country Phone Code*" beside "Phone number*"; Unit8: a tel box whose own
    label reads "Phone (Optional) +41". Both rejected the full "+41 79 ..." on 2026-08-27."""
    with_select = {"fields": [
        _field("e1", "Country Phone Code*", "select",
               options_sample=["Please Select", "Afghanistan (+93)", "Switzerland (+41)"]),
        _field("e2", "Phone number*", "text"),
    ]}
    plan, _ = rules.plan_for(with_select, "en")
    by_ref = {s["ref"]: s for s in plan}
    assert by_ref["e1"]["labels"][0] == "+41"
    assert "Switzerland (+41)" in by_ref["e1"]["labels"]
    assert not by_ref["e2"]["value"].startswith("+")
    assert by_ref["e2"]["mode"] == "insert"

    in_label = {"fields": [_field("e1", "Phone (Optional) +41", "tel")]}
    plan, _ = rules.plan_for(in_label, "en")
    assert not plan[0]["value"].startswith("+")

    alone = {"fields": [_field("e1", "Telefon", "tel")]}
    plan, _ = rules.plan_for(alone, "de")
    assert plan[0]["value"].startswith("+")          # nothing else supplies the code


def test_a_yes_no_fact_offers_only_options_that_say_the_same_thing():
    assert rules.option_candidates("criminal_record", "no", "de", None)[0] == "Nein"
    assert "Nein" not in rules.option_candidates("criminal_record", "yes", "de", None)


def test_facts_without_an_answer_are_left_for_review_not_guessed(monkeypatch):
    """`criminal_or_debt_record` is the applicant's to state. With no answer key the field
    is audited `missing_profile`, never written."""
    from harness.ops.profile import ApplicantProfile
    monkeypatch.setattr(rules, "APPLICANT", ApplicantProfile())
    schema = {"fields": [_field("e1", "Eintrag im Straf- und Betreibungsregister *",
                                "select", options_sample=["---", "Ja", "Nein"])]}
    plan, audit = rules.plan_for(schema, "de")
    assert plan == []
    assert audit[0]["status"] == "missing_profile"


def test_a_choice_control_labelled_phone_is_the_country_code_picker():
    """Unit8: a `role=combobox` labelled "Phone (Optional) +41" offered "United States +1 |
    Germany +49 | …". A select cannot take a number; it chooses the prefix."""
    assert classify("Phone (Optional) +41", kind="combobox") == "phone_country_code"
    assert classify("Telefon", kind="select") == "phone_country_code"


def test_a_twin_anywhere_in_the_schema_and_a_semantic_twin_are_both_skipped():
    schema = {"fields": [
        _field("e1", "Höchster Bildungsabschluss*", "select",
               options_sample=["Hauptschule", "Allgemeine Hochschulreife", "Hochschulabschluss"]),
        _field("e2", "Berufserfahrung*", "select", options_sample=["Keine", "6-10 Jahre"]),
        _field("e3", "Schule, Ausbildung, Beruf", "combobox", needs_interaction=True),
        _field("e4", "Höchster Bildungsabschluss*", "combobox", needs_interaction=True),
    ]}
    plan, audit = rules.plan_for(schema, "de")
    statuses = {a["ref"]: a["status"] for a in audit}
    assert statuses["e3"] == "decorator_twin" and statuses["e4"] == "decorator_twin"
    assert [s["ref"] for s in plan] == ["e1", "e2"]


def test_education_prefers_the_rung_the_select_offers():
    """Exact-match label lists never spelled "1. Master Université" or "Hochschulabschluss"
    the way a given select does; three forms rejected the CV's own degree title."""
    vaudoise = _field("e1", "Veuillez renseigner votre niveau d'étude le plus élevé *",
                      "select", options_sample=["", "1. Master Université",
                                                "2. Bachelor Université", "3. Master Haute Ecole",
                                                "4. Bachelor Haute Ecole"])
    plan, _ = rules.plan_for({"fields": [vaudoise]}, "fr")
    assert plan[0]["labels"][:2] == ["1. Master Université", "3. Master Haute Ecole"]

    adesso = _field("e1", "Höchster Bildungsabschluss*", "select",
                    options_sample=["Hauptschule", "Realschule", "Berufsausbildung",
                                    "Fachhochschulreife", "Allgemeine Hochschulreife",
                                    "Hochschulabschluss"])
    plan, _ = rules.plan_for({"fields": [adesso]}, "de")
    assert plan[0]["labels"][0] == "Hochschulabschluss"

    no_rung = _field("e1", "Schulabschluss *", "select",
                     options_sample=["Kein Abschluss", "Hauptschulabschluss", "Mittlere Reife"])
    plan, _ = rules.plan_for({"fields": [no_rung]}, "de")
    assert not any(label in ("Kein Abschluss", "Hauptschulabschluss", "Mittlere Reife")
                   for label in plan[0]["labels"])          # never a rung below the truth
