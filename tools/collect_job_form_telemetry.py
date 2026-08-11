"""Dry-run 100 job application forms through browser-harness.

Run through the harness namespace, not as ordinary Python:

    BH_JOURNAL=outputs/job-form-telemetry-2026-08-08/journal.jsonl \
      uv run bh < tools/collect_job_form_telemetry.py

The harness blocks submission. This script fills only facts supported by the CV and records
missing information rather than inventing it. CV upload is opt-in via
``BH_APPLICATION_UPLOADS=1`` because choosing a local file can transfer it to an employer
before a form is submitted. Results are crash-safe JSONL plus one input-ordered JSON
document.
"""
# The harness injects these helpers into the script namespace at runtime. Per-job broad
# catches are deliberate: one hostile page must become a typed record, not erase 99 peers.
# ruff: noqa: F821, BLE001
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

from harness.ops.profile import ApplicantProfile, ProfileValue, load_answer_file

ROOT = Path.cwd()
INPUT = ROOT / "jobs.json"
OUT = Path(os.environ.get(
    "BH_APPLICATION_TELEMETRY_OUT",
    ROOT / "outputs" / "job-form-telemetry-2026-08-08",
))
CV = Path(
    "/Users/rebourne/Desktop/Dev/jobsuche-101/Bewerbung2026/"
    "Lebenslauf/Lebenslauf – Enes Poyraz.pdf"
)
WORKERS = int(os.environ.get("BH_APPLICATION_WORKERS", "10"))
MAX_HOPS = 2
UPLOAD_CV = os.environ.get("BH_APPLICATION_UPLOADS", "").strip().lower() in {
    "1", "true", "yes",
}

PROFILE = {
    "first_name": "Enes",
    "last_name": "Poyraz",
    "full_name": "Enes Poyraz",
    "email": "epoyraz.eth@gmail.com",
    "phone": "+41 77 266 98 58",
    "street": "Büchelerstrasse 12",
    "street_name": "Büchelerstrasse",
    "house_number": "12",
    "postal_code": "8212",
    "city": "Neuhausen am Rheinfall",
    "country_en": "Switzerland",
    "country_de": "Schweiz",
    "country_fr": "Suisse",
    "nationality_en": "Swiss",
    "nationality_de": "Schweiz",
    "nationality_fr": "Suisse",
    "birth_date_iso": "1987-02-05",
    "birth_date_local": "05.02.1987",
    "birth_place": "Gelsenkirchen, Deutschland",
    "current_company": "Xona AI",
    "current_title": "Co-Founder & Full-Stack Software Engineer",
    "headline": "Senior Full-Stack Software Engineer & Co-Founder",
    "experience_years": "8+",
    "location": "Neuhausen am Rheinfall, Switzerland",
    "education": "M.Sc. Bioinformatik",
    "english": "Fluent",
    "german": "Native",
    "turkish": "Fluent",
    "summary": (
        "Senior Full-Stack Software Engineer with over 8 years of experience building "
        "and operating cloud-native web platforms with C#/.NET, TypeScript/Next.js and "
        "Microsoft Azure. Hands-on co-founder of an AI SaaS platform with strengths in "
        "identity and access management, Kubernetes, Terraform and CI/CD."
    ),
}

REQUIRED = ROOT / "required.txt"
APPLICANT = ApplicantProfile.from_mapping(PROFILE, source=str(CV))
if REQUIRED.is_file():
    APPLICANT = APPLICANT.merged(load_answer_file(REQUIRED))

ANSWER_KEYS = {
    "salary_expectation": "salary_expectation_chf_gross_per_year",
    "availability": "availability",
    "linkedin_url": "linkedin_url",
    "github_url": "github_url",
    "portfolio_url": "portfolio_url",
    "consent": "required_privacy_consent",
    "referral_source": "referral_source_priority",
}


def norm(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(value or ""))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#./ -]+", " ", raw)).strip()


def field_text(field: dict[str, Any]) -> str:
    return norm(" ".join(str(field.get(k) or "") for k in ("label", "name", "kind")))


def inferred_required(field: dict[str, Any]) -> bool:
    label = str(field.get("label") or "").lower()
    return bool(field.get("required") or "*" in label or "required" in label
                or "erforderlich" in label or "requis" in label)


SEMANTIC_CACHE: dict[tuple[Any, ...], str] = {}
SEMANTIC_CACHE_HITS = 0
SEMANTIC_CACHE_LOCK = threading.Lock()


def _semantic_uncached(field: dict[str, Any]) -> str:
    """A deliberately small, cross-ATS ontology. Specific patterns win first."""
    text = field_text(field)
    label = norm(field.get("label"))
    name = norm(field.get("name"))
    kind = field.get("kind") or ""

    if any(x in text for x in ("linkedin", "linked in")):
        return "linkedin_url"
    if any(x in text for x in ("github", "gitlab")):
        return "github_url"
    if any(x in text for x in ("salary", "gehalt", "lohn", "compensation", "per annum")):
        return "salary_expectation"
    if any(x in text for x in ("notice period", "start date", "starting date", "available from",
                               "availability", "verfugbar", "fruhest", "a partir de quand")):
        return "availability"
    if any(x in text for x in ("portfolio", "personal website", "website url", "homepage")):
        return "portfolio_url"
    if any(x in text for x in ("motivation", "why do you", "why are you", "why join",
                               "cover letter", "covering letter", "impressive thing",
                               "anything else", "type your response")):
        return "tailored_response"
    if any(x in text for x in ("how did you hear", "how did you find", "aufmerksam geworden",
                               "contacttype", "recommendation source", "referral")):
        return "referral_source"
    if any(x in text for x in ("privacy", "datenschutz", "data protection", "gdpr",
                               "consent", "accepted data", "disclaimer")):
        return "consent"
    if any(x in text for x in ("gender", "geschlecht", "sexe", "salutation", "anrede")):
        return "gender_or_salutation"
    if any(x in text for x in ("race", "ethnicity", "veteran", "disability", "demographic")):
        return "demographic"
    if any(x in text for x in ("email bestatigen", "verify email", "confirm email", "email2")):
        return "email_confirm"
    if kind == "email" or name in {"email", "candidate email", "awsm applicant email"}:
        return "email"
    if any(x in text for x in ("first name", "firstname", "vorname", "prenom")):
        return "first_name"
    if any(x in text for x in ("last name", "lastname", "surname", "nachname", "nom requis")):
        return "last_name"
    if any(x in text for x in ("full name", "prenom et nom", "applicant name")):
        return "full_name"
    if kind == "tel" or any(x in text for x in ("phone", "telefon", "telephone", "mobilephone")):
        return "phone"
    if any(x in text for x in ("date of birth", "birthdate", "geburtsdatum", "date de naissance")):
        return "birth_date"
    if any(x in text for x in ("place of birth", "geburtsort", "lieu de naissance")):
        return "birth_place"
    if any(x in text for x in ("nationality", "nationalitat", "nationalities", "nationalitaten")):
        return "nationality"
    if any(x in text for x in ("work permit", "work authorization", "work authorisation",
                               "arbeitsbewilligung", "arbeitsgenehmigung", "swiss citizen",
                               "can you work in switzerland")):
        return "work_authorization"
    if any(x in text for x in ("postal code", "postcode", "zip", "postleitzahl", " plz")):
        return "postal_code"
    if any(x in text for x in ("house number", "hausnummer", " nr ", "cust number")):
        return "house_number"
    if name == "country" or any(x in label for x in ("country", "land", "pays")):
        return "country"
    if any(x in text for x in ("current location", "residence", "wohnort", "lieu de residence")):
        return "location"
    if name == "city" or any(x in label for x in ("city", "ort", "ville")):
        return "city"
    if any(x in text for x in ("street", "strasse", "address", "adresse postale")):
        return "street"
    if any(x in text for x in ("current company", "current employer")) or name == "org":
        return "current_company"
    if any(x in text for x in ("current title", "current role", "job title", "headline")):
        return "current_title"
    if any(x in text for x in ("years of experience", "jahre berufserfahrung",
                               "professional experience", "softwareentwicklung hast")):
        return "experience_years"
    if any(x in text for x in ("english level", "english proficiency", "englischniveau")):
        return "english_level"
    if any(x in text for x in ("german level", "german proficiency", "deutschniveau")):
        return "german_level"
    if any(x in text for x in ("highest education", "degree", "education")):
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
    key = (norm(field.get("label")), norm(field.get("name")), field.get("kind"),
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
        "experience_years": ["8+", "8+ years", "7+ years", "7+ Jahre", "5+ Jahre"],
        "english_level": ["C1/C2", "C2", "C1", "Fluent", "Native or bilingual"],
        "german_level": ["Native", "Muttersprache", "C2", "C1/C2"],
        "work_authorization": ["Schweizer/-in", "Swiss citizen", "Ja", "Yes"],
        "country": ["Schweiz", "Switzerland", "Suisse"],
        "nationality": ["Schweizer/-in", "Swiss", "Schweiz", "Suisse"],
        "timezone": ["Europe/Zurich", "UTC+1", "CET"],
        "consent": ["Ja", "Yes", "Oui"],
    }
    answer = str(value)
    out = [answer, *candidates.get(semantic_name, [])]
    if semantic_name == "availability":
        out.extend(["Per sofort", "Immediately", "Immédiatement"])
    if semantic_name == "gender_or_salutation":
        out.extend(["Herr", "Mr", "Monsieur"])
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
    for index, field in enumerate(schema.get("fields") or []):
        sem = semantic(field)
        required = inferred_required(field)
        base = {
            "index": index, "ref": field.get("ref"), "kind": field.get("kind"),
            "label": field.get("label"), "name": field.get("name"),
            "required": required, "semantic": sem,
        }
        group = str(field.get("name") or field.get("label") or f"field-{index}")
        if field.get("kind") in ("radio", "checkbox") and sem == "unclassified":
            if group in seen_radio_groups:
                continue
            seen_radio_groups.add(group)
        item = profile_value(sem, language)
        if item is not None and item.known_absent:
            audit.append({**base, "status": "known_absent", "value_source": item.source})
            continue
        value = localized(sem, language, field)
        if value is None and sem in EXTRA_PROFILE:
            audit.append({**base, "status": "missing_profile"})
            continue
        if value is None or not field.get("ref"):
            audit.append({**base, "status": "unclassified"})
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


def cv_inputs(file_inputs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not file_inputs:
        return [], []
    cv_words = ("cv", "resume", "résumé", "lebenslauf", "curriculum")
    scored = [f for f in file_inputs if any(w in norm(f"{f.get('name')} {f.get('label')}") for w in cv_words)]
    if scored:
        return scored, [f for f in file_inputs if f not in scored]
    if len(file_inputs) == 1:
        return file_inputs, []
    return [], file_inputs


def compact_error(error: Exception) -> dict[str, Any]:
    outcome = getattr(error, "outcome", None)
    return {
        "class": getattr(getattr(error, "cls", None), "value", type(error).__name__),
        "error": str(error)[:300],
        "outcome": outcome.to_json() if outcome is not None else None,
    }


def add_diagnostics(result: dict[str, Any]) -> None:
    try:
        with session.journal.bind(stage="diagnostics"):
            result["diagnostics"] = session.tab().diagnostics()
    except Exception as error:
        result["diagnostics"] = {"error": compact_error(error)}


def one_job(job: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    apply = job.get("apply") or {}
    start_url = apply.get("direct_url") or job.get("url")
    result: dict[str, Any] = {
        "rank": job.get("rank"), "job_id": job.get("job_id"),
        "company": job.get("company"), "title": job.get("title"),
        "ats": apply.get("ats"), "declared_mode": apply.get("mode"),
        "start_url": start_url, "hops": [], "errors": [],
    }
    with session.journal.bind(stage="diagnostics"):
        result["diagnostics_started"] = session.tab().start_diagnostics()
    try:
        application = session.run_application(
            start_url, timeout=25, transition_timeout=15, hop_budget=6,
            candidates=application_route_candidates(start_url), planner=plan_for)
    except Exception as error:
        error_class = getattr(getattr(error, "cls", None), "value", type(error).__name__)
        result["status"] = ("navigation_failed" if error_class == "navigation_failed"
                            else "workflow_failed")
        result["errors"].append(compact_error(error))
        add_diagnostics(result)
        result["wall_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return result

    workflow = application["location"]
    prepared = workflow["prepared"]
    skill_packet = application.get("skills") or {}
    result["skills"] = {
        "enabled": bool(skill_packet.get("enabled")),
        "matches": skill_packet.get("matches") or [],
        "bytes": int(skill_packet.get("bytes") or 0),
        "sha256": skill_packet.get("sha256"),
    }
    result["navigation"] = workflow["navigation"]
    result["navigate_ms"] = workflow["wall_ms"]
    result["workflow_terminal"] = workflow["terminal_state"]
    result["hops"] = workflow["hops"]

    schema = prepared.get("schema") or {}
    result["landed_url"] = prepared.get("url") or result.get("navigation", {}).get("landed")
    result["language"] = prepared.get("language")
    result["context"] = prepared.get("context")
    result["contexts_checked"] = prepared.get("contexts_checked")
    result["schema"] = schema
    result["is_application"] = bool(prepared.get("is_application"))
    if not result["is_application"]:
        result["status"] = "no_application_form"
        add_diagnostics(result)
        result["wall_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return result

    plan, audit = application["plan"], application["audit"]
    result["field_audit"] = audit
    result["fill_plan_count"] = len(plan)
    result["fill_ms"] = application.get("fill_ms")
    result["fill"] = application.get("fill")

    file_inputs = prepared.get("file_inputs") or []
    chosen, ambiguous = cv_inputs(file_inputs)
    result["file_inputs"] = file_inputs
    result["ambiguous_file_inputs"] = ambiguous
    uploads = []
    if UPLOAD_CV:
        for item in chosen:
            try:
                t0 = time.perf_counter()
                outcome = upload_file(item["ref"], str(CV), timeout=25)
                uploads.append({"input": item, "ms": round((time.perf_counter() - t0) * 1000, 1),
                                "outcome": outcome.to_json()})
            except Exception as error:
                uploads.append({"input": item, "error": compact_error(error)})
    result["uploads"] = uploads
    result["upload_skipped"] = bool(chosen) and not UPLOAD_CV
    result["status"] = "form_processed"
    add_diagnostics(result)
    result["wall_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return result


def main() -> None:
    if not INPUT.is_file() or not CV.is_file():
        raise RuntimeError(f"missing input: jobs={INPUT.is_file()} cv={CV.is_file()}")
    OUT.mkdir(parents=True, exist_ok=True)
    jobs = json.loads(INPUT.read_text(encoding="utf-8"))["jobs"]
    if len(jobs) != 100:
        raise RuntimeError(f"expected 100 jobs, got {len(jobs)}")
    completed_path = OUT / "results-completion-order.jsonl"
    completed_path.write_text("", encoding="utf-8")
    write_lock = threading.Lock()
    run_started = time.time()

    completion_sequence = 0

    def progress(done: int, total: int, record: dict[str, Any]) -> None:
        nonlocal completion_sequence
        with write_lock:
            completion_sequence += 1
            with completed_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"sequence": completion_sequence, **record},
                                    ensure_ascii=False, default=str) + "\n")
        if done == 1 or done % 5 == 0 or done == total:
            value = record.get("value") or {}
            print(f"progress {done}/{total} rank={value.get('rank')} status={value.get('status')}", flush=True)

    records = parallel(jobs, one_job, workers=WORKERS, reuse_tabs=True, isolated=False,
                       timeout=30 * 60, progress=progress,
                       item_id=lambda job: job["job_id"])
    summary = summarise(records)
    summary.pop("values", None)  # records below are authoritative; do not duplicate them.
    payload = {
        "meta": {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "input": str(INPUT), "cv": str(CV), "workers_requested": WORKERS,
            "workers_effective": min(WORKERS, 10), "dry_run": True,
            "submissions": 0, "cv_uploads_enabled": UPLOAD_CV,
            "wall_ms": round((time.time() - run_started) * 1000, 1),
            "profile_sources": sorted({item.source for item in APPLICANT.values.values()}),
            "model_boundary": {
                "scripted": True, "model_calls": 0, "input_tokens": 0,
                "output_tokens": 0, "decision_packet_fields": sorted(APPLICANT.values),
                "application_skills": os.environ.get("BH_APPLICATION_SKILLS", "1"),
                "skill_context_delivered_to_planner": True,
                "skill_context_interpreted": False,
                "decision_packet_sha256": hashlib.sha256("\n".join(
                    sorted(APPLICANT.values)).encode()).hexdigest()[:16],
            },
            "semantic_cache": {"strategies": len(SEMANTIC_CACHE),
                               "hits": SEMANTIC_CACHE_HITS, "stores_live_refs": False},
        },
        "parallel_summary": summary,
        "records": records,
    }
    (OUT / "results.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    print(json.dumps({"output": str(OUT / "results.json"),
                      "summary": payload["parallel_summary"],
                      "wall_ms": payload["meta"]["wall_ms"]}, default=str))


if __name__ in {"__main__", "__bh__"}:
    main()
