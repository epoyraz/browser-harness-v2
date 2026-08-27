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


def test_a_repeat_field_is_a_mail_confirmation_only_when_it_is_about_mail():
    """"erneut eingeben" alone is how a form asks for a repeated *password*."""
    assert classify("E-Mail-Adresse erneut eingeben: *") == "email_confirm"
    assert classify("Kennwort erneut eingeben: *", "fbclc_pwdConf",
                    "password") != "email_confirm"
