"""A `plan_for` that asks a model, so the planner slot can be measured with one in it.

`run_application(url, planner=...)` has always taken a callback; every corpus run so far
filled it with a rule table and reported `model_calls: 0`. This is the same interface
backed by the Codex CLI, which makes the comparison the harness was built for possible:
the same forms, the same scoring, one planner that matches patterns and one that reasons.

**What this sends is field descriptions and the *names* of available answers** —
`email`, `postal_code`, `salary_expectation` — never their values. Substitution happens
here, afterwards. The model needs to know that an answer for "E-Mail-Adresse erneut
eingeben" exists; it does not need to know what it is.

**That is a property of the payload, not a sandbox, and the difference matters.** The child
is a coding agent with filesystem access, launched in this repository, which holds the
applicant profile and the answer file. `--sandbox read-only` governs the shell commands the
model may run, not what it may read. Employer-controlled labels and options go into its
prompt, so an injected instruction — or ordinary exploration — could read those files into
its context, and no flag here prevents it. `--ignore-user-config` and a working directory
holding nothing but the packet close most of that; a tool-free structured-output call
against the API would close it properly, and is the right shape for this to take.

Until then, treat the guarantee as "no values are *sent*", which is what the telemetry
field `values_sent_to_model` claims and all it should be read to claim.

One call per form, not per field: a form is one question about fifteen fields, and asking
it fifteen times is how a decision loop gets expensive.

Answers are cached by a digest of the fields, so re-scoring a corpus costs nothing and a
run can resume.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODEL = os.environ.get("BH_PLANNER_MODEL", "gpt-5.6-luna")
EFFORT = os.environ.get("BH_PLANNER_EFFORT", "max")
CACHE = Path(os.environ.get("BH_PLANNER_CACHE", ROOT / "outputs" / "planner-cache"))
TIMEOUT = float(os.environ.get("BH_PLANNER_TIMEOUT", "600"))
#: Bumped when the planner's own interpretation of an answer changes in a way the
#: instructions and schema do not capture.
CACHE_VERSION = 2

# The ontology is a package now, so the model planner imports it rather than loading a
# benchmark script to reach the project's application knowledge.
from applications import ontology as _rules

#: Re-exported so a scorer can load this module in place of the rule table.
semantic = _rules.semantic
norm = _rules.norm
inferred_required = _rules.inferred_required

_calls = 0
_cache_hits = 0
_lock = threading.Lock()
#: The classifier swap below is module-global in `_rules`, so only one plan at a time.
_patch_lock = threading.Lock()

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["fields"],
    "properties": {
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                # Strict structured output requires every property to be listed in
                # `required`, so the optional ones are nullable rather than absent.
                "required": ["ref", "verdict", "key"],
                "properties": {
                    "ref": {"type": "string"},
                    "verdict": {
                        "type": "string",
                        "enum": ["answer", "judgement", "credential", "skip"],
                    },
                    "key": {"type": ["string", "null"]},
                },
            },
        },
    },
}

INSTRUCTIONS = """\
You map job-application form fields to answers that are already on file. You never see the
answers themselves, only their names, and you must not invent any.

For each field return exactly one verdict:
  answer     - the field wants one of the available answers. Give "key". This covers
               selects and radio groups too: say WHICH answer the field wants and the
               harness picks the matching option itself. You are not shown the answers, so
               you could not pick between "Herr" and "Frau" anyway — that is deliberate.
  judgement  - the field asks for an opinion, an essay, a self-assessment, a rating, or any
               answer that is not a stored fact. Skill-level questions are always judgement.
  credential - a password or any other secret. Never answer these.
  skip       - the field is decoration, a duplicate, or you cannot tell what it wants.

Rules that matter more than coverage:
  - A wrong answer is far worse than "skip". If unsure, skip.
  - `group_label` is the question a radio or checkbox group asks; the field's own `label` is
    only one option answering it. Judge by the group's question.
  - `label_source: proximity` means the label was guessed from page geometry and may belong
    to a different control. Trust it less than "markup".
  - Fields are in the language given. Answer keys are English; match meaning, not spelling.
  - Do not answer demographic questions (race, ethnicity, gender identity, veteran status,
    disability) - those are the applicant's to answer. Use "skip".

Return one entry per field, using the ref exactly as given.
"""


def _describe(field: dict[str, Any]) -> dict[str, Any]:
    """The field as the model sees it. No values, no refs into the live document."""
    out = {
        "ref": field.get("ref"),
        "kind": field.get("kind"),
        "label": field.get("label"),
        "required": bool(inferred_required(field)),
    }
    for key in ("group_label", "name", "label_source"):
        if field.get(key):
            out[key] = field[key]
    options = field.get("options_sample") or []
    if options:
        out["options"] = [str(o) for o in options][:24]
        if field.get("options_count"):
            out["options_count"] = field["options_count"]
    return out


#: Entries in the answer file that state policy rather than supply an answer. Offering
#: them as things a form field could contain invites exactly the mapping they forbid.
_POLICY_KEYS = frozenset({"answer_sources", "unsupported_answer_policy",
                          "submit_applications"})


def _answer_keys() -> list[str]:
    return sorted(k for k in _rules.APPLICANT.values if k not in _POLICY_KEYS)


def _ask(payload: dict[str, Any]) -> dict[str, Any]:
    """One Codex call. Cached by the digest of exactly what was sent."""
    global _calls, _cache_hits
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    # The whole semantic contract, not just the payload: a fix to the instructions or the
    # output schema changes what the answer means, and a digest blind to them silently
    # serves the answer to the old question.
    contract = hashlib.sha256(
        (INSTRUCTIONS + json.dumps(OUTPUT_SCHEMA, sort_keys=True)).encode()
    ).hexdigest()[:12]
    digest = hashlib.sha256(
        f"{CACHE_VERSION}|{MODEL}|{EFFORT}|{contract}|{blob}".encode()).hexdigest()[:32]
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = CACHE / f"{digest}.json"
    if cached.is_file():
        try:
            answer = json.loads(cached.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            answer = None            # a half-written entry is a miss, not a crash
        if isinstance(answer, dict) and "fields" in answer:
            with _lock:
                _cache_hits += 1
            return answer

    schema_file = CACHE / f"{digest}.schema.json"
    schema_file.write_text(json.dumps(OUTPUT_SCHEMA), encoding="utf-8")
    prompt = f"{INSTRUCTIONS}\n\nINPUT:\n{blob}\n"
    # An empty working directory and no user configuration: the child is a coding agent
    # with filesystem access, and this repository holds the applicant profile, the answer
    # file and CV-derived material. Running it here meant an employer-controlled label in
    # its prompt could steer it into reading them. This does not make it a sandbox — see
    # the module docstring — but it removes the applicant data from what it can reach.
    with tempfile.TemporaryDirectory(prefix="bh-planner-") as workdir:
        command = [
            "codex", "exec", "--model", MODEL,
            "-c", f"model_reasoning_effort={EFFORT}",
            "--output-schema", str(schema_file),
            "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
            "--ignore-user-config", "--cd", workdir,
            "-",
        ]
        try:
            result = subprocess.run(command, input=prompt, text=True, encoding="utf-8",
                                    capture_output=True, timeout=TIMEOUT, check=False)
        finally:
            schema_file.unlink(missing_ok=True)
    with _lock:
        _calls += 1
    if result.returncode != 0:
        raise RuntimeError(f"codex exited {result.returncode}: {result.stderr[-400:]}")
    answer = _last_json_object(result.stdout)
    if answer is None:
        raise RuntimeError(f"no JSON object in codex output: {result.stdout[-400:]}")
    # Atomic: a crash mid-write must not leave a file that later reads as a hit.
    staging = cached.with_suffix(f".{os.getpid()}.tmp")
    staging.write_text(json.dumps(answer, ensure_ascii=False), encoding="utf-8")
    os.replace(staging, cached)
    return answer


def _last_json_object(text: str) -> dict[str, Any] | None:
    """`codex exec` prints progress around its final message; take the last JSON object."""
    best = None
    for start in range(len(text)):
        if text[start] != "{":
            continue
        for end in range(len(text), start, -1):
            if text[end - 1] != "}":
                continue
            try:
                candidate = json.loads(text[start:end])
            except ValueError:
                continue
            if isinstance(candidate, dict) and "fields" in candidate:
                best = candidate
            break
    return best


def stats() -> dict[str, int]:
    return {"model_calls": _calls, "cache_hits": _cache_hits}


def plan_for(schema: dict[str, Any], language: str,
             skill_context: dict[str, Any] | None = None
             ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Same contract as the rule planner, and the same body — only the classifier differs.

    Everything after "what does this field want" is shared: refusing credentials, answering
    a radio group once through the option that matches, declining when no option does,
    resolving a key to a value. Reusing it makes the comparison honest, because the two
    planners then differ in exactly one place — `semantic()` — rather than in a second
    implementation of the parts that were already measured.
    """
    del skill_context                       # prose interpretation is a separate question
    fields = [f for f in (schema.get("fields") or []) if f.get("ref")]
    if not fields:
        return [], []
    answer = _ask({
        "language": language,
        "available_answer_keys": _answer_keys(),
        "fields": [_describe(f) for f in fields],
    })
    verdicts = {str(v.get("ref")): v for v in (answer.get("fields") or [])}

    def classify(field: dict[str, Any]) -> str:
        verdict = verdicts.get(str(field.get("ref"))) or {}
        if str(verdict.get("verdict")) != "answer":
            return "unclassified"
        key = str(verdict.get("key") or "")
        return key if key in _rules.APPLICANT.values else "unclassified"

    judged = {str(f.get("ref")) for f in fields
              if str((verdicts.get(str(f.get("ref"))) or {}).get("verdict")) == "judgement"}

    with _patch_lock:
        rule_semantic, rule_matrix = _rules.semantic, _rules.rating_matrix
        _rules.semantic = classify
        # The model's own read of "this needs judgement" replaces the structural guess.
        _rules.rating_matrix = lambda _schema: judged
        try:
            plan, audit = _rules.plan_for(schema, language)
        finally:
            _rules.semantic, _rules.rating_matrix = rule_semantic, rule_matrix
    for row in audit:
        row["planner"] = "model"
    return plan, audit
