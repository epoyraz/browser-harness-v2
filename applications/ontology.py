"""What a job-application field is asking for, and what to write into it.

Every pattern here was earned from a real posting, and every one is knowledge about
recruiting software rather than about browsers: that Swiss forms spell it "E-Mail", that
"Anredetitel" offers Dr./Prof. while "Anrede" offers Herr/Frau, that a run of short
required fields is a skill matrix nobody can answer from a CV.

It sits above the harness for exactly that reason. `form_schema()` reports what a control
is — its label, its options, where that label came from — and stops. Deciding that a
control labelled "Höchster Bildungsabschluss" wants the education answer is judgment about
a domain, and the harness holds none of it.

Moved out of `tools/collect_job_form_telemetry.py`, where a benchmark script had become the
only home for the project's application knowledge.
"""
from __future__ import annotations

import re
import threading
import unicodedata
from typing import Any

from harness.ops.profile import ApplicantProfile, ProfileValue

ANSWER_KEYS = {
    "salary_expectation": "salary_expectation_chf_gross_per_year",
    "availability": "availability",
    "linkedin_url": "linkedin_url",
    "github_url": "github_url",
    "portfolio_url": "portfolio_url",
    "consent": "required_privacy_consent",
    "referral_source": "referral_source_priority",
}


#: The applicant's own data is not ontology. What a field is asking for is knowledge about
#: recruiting software and belongs here; what to answer is one person's, supplied by
#: whoever is running the thing. Keeping them apart is what lets this module be read,
#: tested and corrected without a CV anywhere near it.
APPLICANT: ApplicantProfile = ApplicantProfile()
PROFILE: dict[str, Any] = {}
CV: str = ""

SEMANTIC_CACHE: dict[tuple[Any, ...], str] = {}
SEMANTIC_CACHE_HITS = 0
SEMANTIC_CACHE_LOCK = threading.Lock()


def configure(applicant: ApplicantProfile, profile: dict[str, Any], cv: str) -> None:
    """Hand the ontology the answers it should map fields onto."""
    global APPLICANT, PROFILE, CV
    APPLICANT, PROFILE, CV = applicant, dict(profile), str(cv)
    with SEMANTIC_CACHE_LOCK:
        SEMANTIC_CACHE.clear()



def norm(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(value or ""))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#./ -]+", " ", raw)).strip()


def field_text(field: dict[str, Any]) -> str:
    # `group_label` carries the question a radio or checkbox group asks; the field's own
    # label is only the option answering it. Without the question, "Novice" is all the
    # ontology gets to work with.
    return norm(" ".join(str(field.get(k) or "")
                         for k in ("label", "group_label", "name", "kind")))


def has_word(text: str, *words: str) -> bool:
    """Match a token as a word, not as a fragment of one.

    Plain `in` is fine for a phrase and dangerous for a short token. Measured on the
    2026-08-25 corpus: a required French field reading "Veuillez indiquer vos prétentions
    salariales annuelles ... (format : 75000)" was classified `city`, because `"ort" in
    "rapportees"` — and it was not merely misread, it was *planned*, so the run wrote
    "Neuhausen am Rheinfall" into a salary box. `land` inside "Switzerland" and `formation`
    inside "information" are the same shape of accident waiting to happen.
    """
    return any(re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text)
               for word in words)


def inferred_required(field: dict[str, Any]) -> bool:
    label = str(field.get("label") or "").lower()
    return bool(field.get("required") or "*" in label or "required" in label
                or "erforderlich" in label or "requis" in label)


SEMANTIC_CACHE_HITS = 0


SEMANTIC_CACHE_LOCK = threading.Lock()


def _semantic_uncached(field: dict[str, Any]) -> str:
    """A deliberately small, cross-ATS ontology. Specific patterns win first."""
    text = field_text(field)
    label = norm(field.get("label"))
    name = norm(field.get("name"))
    kind = field.get("kind") or ""

    if (has_word(text, "linkedin") or "linked in" in text) and (
            has_word(name, "linkedin") or len(label) <= 30
            # `has_word`, because "link" is a substring of "linkedin": testing it with
            # `in` makes every mention of LinkedIn its own evidence that a LinkedIn URL
            # was requested, which is the circularity this whole check exists to break.
            or has_word(text, "url", "link", "profil", "profile")):
        return "linkedin_url"
    if any(x in text for x in ("github", "gitlab")):
        return "github_url"
    if any(x in text for x in ("salary", "gehalt", "lohn", "compensation", "per annum",
                               "desired pay", "expected pay", "lohnvorstellung",
                               "gehaltsvorstellung", "pretentions salariales", "salaire")):
        return "salary_expectation"
    if any(x in text for x in ("notice period", "start date", "starting date", "available from",
                               "availability", "verfugbar", "fruhest", "a partir de quand",
                               "date available", "earliest start", "kundigungsfrist",
                               "eintrittsdatum", "eintritt", "preavis", "disponibilite",
                               "antreten", "start point", "as of when", "when can you",
                               "wann kannst du", "wann konnen sie")):
        return "availability"
    if any(x in text for x in ("portfolio", "personal website", "website url", "homepage")):
        return "portfolio_url"
    if any(x in text for x in ("motivation", "why do you", "why are you", "why join",
                               "cover letter", "covering letter", "impressive thing",
                               "anything else", "type your response",
                               # The ontology was English-first and so was this line. Half
                               # the essay prompts in the 2026-08-26 corpus are German.
                               "erzahl uns", "erzahle uns", "inwiefern", "weshalb",
                               "was ist fur dich", "wie bringst du", "wie setzt du",
                               "would you consider", "what type of", "what should be",
                               "link/screenshots", "links/screenshots",
                               "parlez-nous", "pourquoi")):
        return "tailored_response"
    if any(x in text for x in ("how did you hear", "how did you find", "aufmerksam geworden",
                               "contacttype", "recommendation source", "referral",
                               "where did you hear", "where did you find",
                               "catch your attention", "auf uns gestossen",
                               "uns gefunden", "auf uns gekommen", "durch wen",
                               "ou avez-vous trouve", "ou avez-vous entendu",
                               "wie haben sie von uns", "wo haben sie")):
        return "referral_source"
    if any(x in text for x in ("privacy", "datenschutz", "data protection", "gdpr",
                               "consent", "accepted data", "disclaimer",
                               "i agree", "ich stimme", "einverstanden",
                               "may be stored", "daten gespeichert", "j accepte")):
        return "consent"
    # An academic title is not a salutation, and "anrede" is a substring of "anredetitel"
    # — so a select offering Dr./Prof. was answered "Herr". What the control *offers*
    # decides it, before any reading of what it is called: one field on this corpus is
    # named `title` and offers Frau/Herr, and another is labelled "Anrede*" by the
    # proximity fallback while its name says `titel-button`. Names and labels disagree
    # here; options do not.
    offered = {norm(option) for option in (field.get("options_sample") or [])}
    if offered & {"herr", "frau", "mr", "mrs", "ms", "monsieur", "madame", "divers"}:
        return "gender_or_salutation"
    if (offered & {"dr.", "dr", "prof.", "prof", "dr. med.", "prof. dr."}
            or any(x in text for x in ("anredetitel", "akademischer titel",
                                       "academic title"))
            or has_word(name, "titel")):
        return "academic_title"
    if (any(x in text for x in ("geschlecht", "salutation"))
            or has_word(text, "gender", "sexe", "anrede")):
        return "gender_or_salutation"
    if any(x in text for x in ("race", "ethnicity", "veteran", "disability", "demographic")):
        return "demographic"
    # "erneut eingeben"/"repeat" only counts as a mail confirmation when the field is
    # actually about mail. Alone it is how a form asks for a repeated *password*, and
    # answering that with an address would type a credential field full of e-mail.
    if (any(x in text for x in ("email bestatigen", "verify email", "confirm email", "email2",
                                "e-mail bestatigen", "email confirmation",
                                "confirmer votre e-mail"))
            or (any(x in text for x in ("erneut eingeben", "repeat", "wiederholen"))
                and any(x in text for x in ("mail", "courriel")))):
        return "email_confirm"
    if (kind == "email" or name in {"email", "candidate email", "awsm applicant email", "mail"}
            or any(x in text for x in ("e-mail", "email", "courriel"))):
        return "email"
    if any(x in text for x in ("first name", "firstname", "vorname", "prenom")):
        return "first_name"
    if (any(x in text for x in ("last name", "lastname", "surname", "nachname",
                                "nom de famille", "nom requis"))
            or has_word(label, "nom")):
        return "last_name"
    if (any(x in text for x in ("full name", "prenom et nom", "applicant name",
                                "vollstandiger name", "vor- und nachname"))
            or (has_word(label, "name", "nom") and has_word(name, "name"))):
        return "full_name"
    if kind == "tel" or any(x in text for x in ("phone", "telefon", "telephone", "mobilephone")):
        return "phone"
    if any(x in text for x in ("date of birth", "birthdate", "birth date", "birthday",
                               "geburtsdatum", "geburtstag", "date de naissance")):
        return "birth_date"
    if any(x in text for x in ("place of birth", "geburtsort", "lieu de naissance")):
        return "birth_place"
    if any(x in text for x in ("nationality", "nationalitat", "nationalities",
                               "nationalitaten", "citizenship", "staatsangehorigkeit",
                               "nationalite")):
        return "nationality"
    if (any(x in text for x in ("work permit", "work authorization", "work authorisation",
                                "arbeitsbewilligung", "arbeitsgenehmigung", "swiss citizen",
                                "can you work in switzerland", "eligible to work",
                                "permis de travail"))
            or has_word(text, "citizen")):
        return "work_authorization"
    if (any(x in text for x in ("postal code", "postcode", "postleitzahl"))
            or has_word(text, "zip", "plz", "npa")):
        return "postal_code"
    # A combined box asks for both parts and wants the whole line. "hausnummer" is a
    # substring of "Strasse und Hausnummer", so testing the number first claimed the
    # combined field and wrote the bare number into it: measured 2026-08-27 on the CSS
    # posting, "Strasse und Hausnummer" received "12" where the street belonged. Only a
    # field that mentions the number *without* the street is the number on its own.
    if ((any(x in text for x in ("house number", "hausnummer", "cust number"))
            or has_word(text, "nr"))
            and not any(x in text for x in ("street", "strasse", "strase", "adresse"))):
        return "house_number"
    if name == "country" or has_word(label, "country", "land", "pays"):
        return "country"
    if any(x in text for x in ("current location", "residence", "wohnort", "lieu de residence")):
        return "location"
    if name == "city" or has_word(label, "city", "ort", "ville", "stadt"):
        return "city"
    if any(x in text for x in ("street", "strasse", "address", "adresse postale")):
        return "street"
    if (any(x in text for x in ("current company", "current employer",
                                "most recently worked", "recent employer",
                                "aktueller arbeitgeber"))
            or name == "org"):
        return "current_company"
    if any(x in text for x in ("current title", "current role", "job title", "headline")):
        return "current_title"
    if (any(x in text for x in ("years of experience", "jahre berufserfahrung",
                                "professional experience", "softwareentwicklung hast",
                                "annees d experience"))
            or has_word(text, "berufserfahrung")):
        return "experience_years"
    if any(x in text for x in ("english level", "english proficiency", "englischniveau",
                               "english fluency", "level of english", "englischkenntnisse",
                               "niveau d anglais")):
        return "english_level"
    if any(x in text for x in ("german level", "german proficiency", "deutschniveau")):
        return "german_level"
    if any(x in text for x in ("study major", "field of study", "studienrichtung",
                               "major & minor", "major and minor", "fachrichtung")):
        return "education"
    if (any(x in text for x in ("highest education", "bildungsabschluss",
                                "hochster abschluss", "bildungsniveau",
                                "niveau d etude", "niveau d etudes"))
            # Short and generic, so they match as words: "formation" is a substring of
            # "information", which every ATS asks for at least once.
            or has_word(text, "education", "degree", "ausbildung", "diplome", "formation")):
        return "education"
    if any(x in text for x in ("profile", "professional summary", "about you", "summary")):
        return "profile_summary"

    # Safe, CV-backed answers to a few common factual screening questions.
    if "restful api" in text:
        return "rest_api_experience"
    if "cloud" in text and any(x in text for x in ("platform", "experience")):
        return "cloud_platform"
    if "time zone" in text or "timezone" in text or "zeitzone" in text:
        return "timezone"

    # Option-only radios can carry the answer even when the question is not exposed.
    if kind == "radio" and label in {"swiss citizen", "schweizer burger", "schweizerin"}:
        return "work_authorization_option"
    return "unclassified"


def semantic(field: dict[str, Any]) -> str:
    """Cache structural meaning, never document-bound refs or current values."""
    global SEMANTIC_CACHE_HITS
    key = (norm(field.get("label")), norm(field.get("group_label")),
           norm(field.get("name")), field.get("kind"),
           tuple(norm(option) for option in (field.get("options_sample") or [])))
    with SEMANTIC_CACHE_LOCK:
        if key in SEMANTIC_CACHE:
            SEMANTIC_CACHE_HITS += 1
            return SEMANTIC_CACHE[key]
    result = _semantic_uncached(field)
    with SEMANTIC_CACHE_LOCK:
        SEMANTIC_CACHE.setdefault(key, result)
    return result


EXTRA_PROFILE = {
    "salary_expectation", "availability", "linkedin_url", "github_url", "portfolio_url",
    "tailored_response", "referral_source", "gender_or_salutation", "demographic", "consent",
}


def profile_value(semantic_name: str, language: str) -> ProfileValue | None:
    key = ANSWER_KEYS.get(semantic_name, semantic_name)
    if semantic_name == "gender_or_salutation":
        key = "salutation_de" if norm(language).startswith("de") else "salutation_en"
    return APPLICANT.get(key)


def option_candidates(semantic_name: str, value: Any, language: str,
                      item: ProfileValue | None) -> list[str]:
    """Ordered exact labels expressing the same supported fact."""
    if item and item.candidates:
        return list(item.candidates)
    lang = norm(language)
    candidates: dict[str, list[str]] = {
        # Swiss forms offer banded ranges far more often than an open "8+": measured
        # 2026-08-27, "Berufserfahrung" offered Keine / 1-2 Jahre / 3-5 Jahre / 6-10 Jahre
        # and matched none of the open-ended forms.
        "experience_years": ["8+", "8+ years", "7+ years", "7+ Jahre", "5+ Jahre",
                             "6-10 Jahre", "6-10 years", "5-10 Jahre", "mehr als 5 Jahre",
                             "More than 5 years", "5+ years"],
        "english_level": ["C1/C2", "C2", "C1", "Fluent", "Native or bilingual",
                          "Professional", "Fliessend", "Verhandlungssicher", "Advanced"],
        "german_level": ["Native", "Muttersprache", "C2", "C1/C2"],
        "work_authorization": ["Schweizer/-in", "Swiss citizen", "Ja", "Yes",
                               "Yes, I have a Swiss passport.", "Schweizer Bürger/-in",
                               "Schweizer Pass", "CH", "Schweiz"],
        "country": ["Schweiz", "Switzerland", "Suisse"],
        "nationality": ["Schweizer/-in", "Swiss", "Schweiz", "Suisse"],
        "timezone": ["Europe/Zurich", "UTC+1", "CET"],
        # A consent control is often its own statement: the option reads "I agree that my
        # data may be stored beyond the current job application", not "Yes". Matching only
        # affirmative words left every one of those reported as `no_option_match`.
        "consent": ["Ja", "Yes", "Oui", "I agree", "Ich stimme zu", "Einverstanden",
                    "Ich bin damit einverstanden", "J'accepte", "Accept", "Akzeptieren"],
    }
    answer = str(value)
    out = [answer, *candidates.get(semantic_name, [])]
    if semantic_name == "availability":
        # A "Kündigungsfrist" select offers notice periods, not start dates, and three
        # forms measured 2026-08-27 reported no_option_match against the immediate
        # wordings alone. Immediate first, then the common Swiss notice periods.
        out.extend(["Per sofort", "Immediately", "Immédiatement", "sofort verfügbar",
                    "Sofort", "ab sofort", "nach Vereinbarung", "3 Monate", "2 Monate",
                    "1 Monat", "3 months", "keine"])
    if semantic_name == "gender_or_salutation":
        # "Mr" without the stop misses "Mr."; a gender select asks a different question
        # with the same semantic, and offers Male/männlich instead of a salutation.
        out.extend(["Herr", "Mr", "Monsieur", "Mr.", "M.", "Male", "männlich", "Mann",
                    "Homme", "Signore"])
    if lang.startswith("de") and semantic_name == "referral_source":
        out.extend(["Unternehmenswebsite", "LinkedIn"])
    return list(dict.fromkeys(part for part in out if part))


def localized(semantic_name: str, language: str, field: dict[str, Any]) -> Any:
    lang = norm(language)
    kind = field.get("kind")
    if semantic_name == "first_name": return PROFILE["first_name"]
    if semantic_name == "last_name": return PROFILE["last_name"]
    if semantic_name == "full_name": return PROFILE["full_name"]
    if semantic_name in ("email", "email_confirm"): return PROFILE["email"]
    if semantic_name == "phone": return PROFILE["phone"]
    if semantic_name == "street": return PROFILE["street"]
    if semantic_name == "house_number": return PROFILE["house_number"]
    if semantic_name == "postal_code": return PROFILE["postal_code"]
    if semantic_name == "city": return PROFILE["city"]
    if semantic_name == "location": return PROFILE["location"]
    if semantic_name == "birth_place": return PROFILE["birth_place"]
    if semantic_name == "current_company": return PROFILE["current_company"]
    if semantic_name == "current_title": return PROFILE["headline"]
    if semantic_name == "experience_years": return PROFILE["experience_years"]
    if semantic_name == "education": return PROFILE["education"]
    if semantic_name == "profile_summary": return PROFILE["summary"]
    if semantic_name == "timezone": return "Europe/Zurich"
    if semantic_name == "rest_api_experience": return "Yes"
    if semantic_name == "cloud_platform": return "Microsoft Azure"
    if semantic_name == "english_level": return "C1/C2" if kind in ("select", "radio") else PROFILE["english"]
    if semantic_name == "german_level": return "Native" if kind in ("select", "radio") else PROFILE["german"]
    if semantic_name == "birth_date":
        return PROFILE["birth_date_iso"] if kind == "date" else PROFILE["birth_date_local"]
    if semantic_name == "country":
        return (PROFILE["country_de"] if lang.startswith("de") else
                PROFILE["country_fr"] if lang.startswith("fr") else PROFILE["country_en"])
    if semantic_name == "nationality":
        return (PROFILE["nationality_de"] if lang.startswith("de") else
                PROFILE["nationality_fr"] if lang.startswith("fr") else PROFILE["nationality_en"])
    if semantic_name == "work_authorization":
        return ("Schweizer Bürger" if lang.startswith("de") else
                "Citoyen suisse" if lang.startswith("fr") else "Swiss citizen")
    if semantic_name == "work_authorization_option": return True
    item = profile_value(semantic_name, language)
    if item is not None and not item.known_absent:
        return item.value
    return None


def _matching_option(group: str, index: int, schema: dict[str, Any], semantic_name: str,
                     value: Any, language: str, item: ProfileValue | None
                     ) -> dict[str, Any] | None:
    """The member of a radio/checkbox group whose own label expresses the answer.

    Exact match first, then a whole-word one, so "Yes" cannot be answered by "Yes, but
    only after my notice period" while "Herr" still matches "Herr:". A group with no
    matching option is not answerable from the profile, and saying so is the honest
    outcome — quietly ticking its first option is how a form gets a wrong answer that
    looks filled.
    """
    if not value:
        return None
    wanted = [norm(candidate) for candidate
              in option_candidates(semantic_name, value, language, item)]
    members = [f for f in (schema.get("fields") or [])
               if f.get("ref") and f.get("kind") in ("radio", "checkbox")
               and str(f.get("name") or f.get("label") or "") == group]
    for exact in (True, False):
        for member in members:
            label = norm(member.get("label"))
            if not label:
                continue
            for candidate in wanted:
                if not candidate:
                    continue
                if label == candidate if exact else has_word(label, candidate):
                    return member
    return None


#: How many unclassified short required fields it takes before a form is asking you to
#: rate a list rather than asking N unrelated questions.
RATING_MATRIX_MIN = 5


def rating_matrix(schema: dict[str, Any]) -> set[str]:
    """Refs belonging to a skill self-rating block.

    One Recruitee employer asked for "Python *", "R *", "SQL *", "Machine Learning *",
    "MLOps *", "DevOps *" and nine more, each a required text field — 45 rows across three
    postings, and the largest single block of unplanned fields in the 2026-08-26 corpus.
    No ontology answers these: they ask what the applicant's level is, which is a judgement
    against the CV rather than a fact in a profile. Reporting them as "unclassified"
    alongside a mislabelled e-mail box conflates two different problems, and only one of
    them is fixable by naming things better.

    The signal is structural, not lexical, because the labels are just nouns: a run of
    short, required, unrecognised fields is a list being rated. Recognising it lets a
    caller ask about all of them in one question instead of one per row.
    """
    candidates = [f for f in (schema.get("fields") or [])
                  if f.get("ref") and f.get("kind") in ("text", "select", "radio",
                                                        "combobox", "textarea")
                  and inferred_required(f)
                  and len(norm(f.get("label"))) <= 40
                  and semantic(f) == "unclassified"]
    return {f["ref"] for f in candidates} if len(candidates) >= RATING_MATRIX_MIN else set()


def plan_for(schema: dict[str, Any], language: str,
             skill_context: dict[str, Any] | None = None
             ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    # The production model planner consumes skill_context. This deterministic corpus
    # planner deliberately does not reinterpret prose; accepting the packet lets the A/B
    # measure routing/injection overhead without changing its factual answer ontology.
    del skill_context
    plan: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    seen_radio_groups: set[str] = set()
    rated = rating_matrix(schema)
    for index, field in enumerate(schema.get("fields") or []):
        sem = semantic(field)
        required = inferred_required(field)
        base = {
            "index": index, "ref": field.get("ref"), "kind": field.get("kind"),
            "label": field.get("label"), "name": field.get("name"),
            "required": required, "semantic": sem,
        }
        # A credential is never corpus data, whatever the label classified as. This is a
        # floor, not a classification: on 2026-08-25 `"ort" in "passwort"` made six SBB,
        # HSLU and fenaco password boxes look like a city field, and all six were planned
        # and written. A guard that does not depend on the ontology being right is the only
        # kind that would have stopped it. Real credentials go through
        # `set_secret_from_keychain`, never through a profile value.
        if field.get("kind") == "password" or has_word(norm(field.get("name")),
                                                       "pwd", "password", "passwort",
                                                       "kennwort"):
            audit.append({**base, "status": "credential_refused"})
            continue
        group = str(field.get("name") or field.get("label") or f"field-{index}")
        if field.get("kind") == "radio" or (field.get("kind") == "checkbox"
                                            and sem == "unclassified"):
            # Radio only for the classified case. Checkboxes that share a name are
            # independent switches — "which of these have you used" wants as many ticks as
            # are true — so collapsing them to one answer would silently drop the rest.
            # One answer per group, always. Every member of a radio group is its own
            # control with its own ref, so a group whose question classifies would
            # otherwise plan a write for each option — selecting all of them in turn and
            # ending on whichever came last. The write has to name the member that says
            # what we mean, so the group is planned only when one of its options matches
            # a supported answer, and left to a human when none does.
            if group in seen_radio_groups:
                continue
            seen_radio_groups.add(group)
            if sem != "unclassified" and field.get("kind") == "radio":
                item = profile_value(sem, language)
                value = localized(sem, language, field)
                chosen = _matching_option(group, index, schema, sem, value, language, item)
                if chosen is not None:
                    # `value` is not optional for a radio: the batch writer does
                    # `el.checked = !!step.value`, so omitting it unchecks the option and
                    # then reports success, because the control is indeed unchecked.
                    plan.append({"ref": chosen["ref"], "value": True})
                    # `base` describes the member being iterated, which is whichever
                    # option came first; the write goes to the one that matches. Carrying
                    # the first member's label next to the chosen member's ref produced an
                    # audit row saying "Frau" about a write to "Herr".
                    audit.append({**base, "ref": chosen["ref"],
                                  "label": chosen.get("label"), "status": "planned",
                                  "value_source": item.source if item else str(CV),
                                  "group_question": field.get("group_label"),
                                  "group_choice": chosen.get("label")})
                    continue
                audit.append({**base, "status": "no_option_match"})
                continue
        item = profile_value(sem, language)
        if item is not None and item.known_absent:
            audit.append({**base, "status": "known_absent", "value_source": item.source})
            continue
        value = localized(sem, language, field)
        if value is None and sem in EXTRA_PROFILE:
            audit.append({**base, "status": "missing_profile"})
            continue
        if value is None or not field.get("ref"):
            audit.append({**base, "status": "needs_judgement" if field.get("ref") in rated
                          else "unclassified"})
            continue
        step: dict[str, Any] = {"ref": field["ref"]}
        if field.get("kind") == "select":
            step["labels"] = option_candidates(sem, value, language, item)
        else:
            step["value"] = value
        if field.get("widget") or field.get("needs_interaction"):
            step["interaction"] = "select"
            step["labels"] = option_candidates(sem, value, language, item)
            step.pop("value", None)
        if sem == "phone" and field.get("kind") != "select":
            step["mode"] = "insert"
        plan.append(step)
        source = item.source if item is not None else str(CV)
        audit.append({**base, "status": "planned", "value_source": source})
    return plan, audit

