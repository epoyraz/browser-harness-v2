"""Replay real form fields through the classifier offline, and count forced decisions.

The metric this exists to move is **forced decisions per application**: a required field the
harness cannot plan is a field the model must be asked about, or an application that goes
out incomplete. Round trips are not that metric — a wasted CDP call costs milliseconds, a
forced decision costs a model call.

The golden set is `field_audit` from a real corpus run: label, name, kind, required and
language for every field the harness actually met, with the classification it actually
produced. Replaying goes through `plan_for` — the planner the run actually used — so
guards and group de-duplication are measured too, not just the pattern table. Rows whose
verdict changed are reported by direction: a field that stopped being planned, and a field
whose confident meaning changed. Both want a human read; neither is automatically wrong.

    uv run python tools/classify_bench.py --out outputs/classify/baseline.json
    uv run python tools/classify_bench.py --out outputs/classify/after.json \
        --baseline outputs/classify/baseline.json

No browser, no network, milliseconds per run.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_GOLDEN = (ROOT / "outputs"
                  / "job-form-telemetry-new-2026-08-25-dry-run-1" / "results.json")

#: An open-ended question has no answer in a CV, so it is a decision the model must make
#: however good the ontology gets. Splitting these out keeps the score honest: the fixable
#: half is the pattern table, and reporting them together would flatter any fix.
OPEN_ENDED = re.compile(
    r"tell us about|describe|what achievement|why do you|why are you|motivat|"
    r"biografie|cover letter|anschreiben|riddle|explain|your thoughts|"
    r"most proud|challenging|proudest|open question|achievements|"
    r"links/screenshots|most relevant work|type your response|your experience with|"
    r"would you feel|looking at your|what are you|have you been in contact|"
    r"we count on|final grades|average grade|what solutions",
    re.IGNORECASE,
)


def load_classifier(path: Path | None = None) -> Any:
    """The corpus planner, imported as a module rather than duplicated here.

    `path` points at another build of it — `git show <rev>:tools/...` — which is the only
    way to attribute a change to the code rather than to the corpus. Two runs over
    different job sets cannot be subtracted from each other.
    """
    spec = importlib.util.spec_from_file_location(
        "collect_job_form_telemetry",
        path or ROOT / "tools" / "collect_job_form_telemetry.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GENERIC_OPTION = re.compile(
    r"^(bitte|please|select|choose|choisir|--|\.\.\.|wählen|auswahl|sélectionner)",
    re.IGNORECASE)


def promoted_label(field: dict[str, Any]) -> str | None:
    """The label `_SCHEMA_JS` now derives from a select's placeholder option.

    Mirrored here so a change already verified against real Chrome can be scored against
    the captured corpus instead of waiting for a fresh 100-job run. It is a projection,
    not a measurement, and the run says how many rows it touched — the next live capture
    replaces it with the real thing.
    """
    options = field.get("options_sample") or []
    if not field.get("placeholder_first") or not options:
        return None
    first = str(options[0] or "").strip()
    if len(first) < 3 or GENERIC_OPTION.match(first) or not re.search(r"[^\W\d_]", first):
        return None
    return first[:120]


def golden_rows(paths: list[Path]) -> list[dict[str, Any]]:
    """Every field the planner saw, with the verdict it reached, per application.

    The rows come from the *schema*, not from `field_audit`. A planner that answers a
    radio group once emits one audit row for the whole group, so replaying from the audit
    hands the group back missing every option except the first — and then scores the run's
    correct choice as a failure to find one. The audit supplies the recorded verdict,
    joined by ref.
    """
    rows: list[dict[str, Any]] = []
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        for record in document.get("records") or []:
            value = record.get("value") or {}
            verdicts = {a.get("ref"): a for a in (value.get("field_audit") or [])}
            if not verdicts:
                continue
            for field in (value.get("schema") or {}).get("fields") or []:
                audit = verdicts.get(field.get("ref")) or {}
                label = field.get("label")
                projected = None if str(label or "").strip() else promoted_label(field)
                rows.append({
                    "job_id": value.get("job_id"),
                    "ats": value.get("ats"),
                    "language": value.get("language") or "en",
                    "label": projected or label,
                    "label_projected": projected is not None,
                    "group_label": field.get("group_label"),
                    "name": field.get("name"),
                    "kind": field.get("kind"),
                    "options_sample": field.get("options_sample"),
                    # A promoted "Anrede*" carries the asterisk the control never had, so
                    # the requirement comes with the label, exactly as in the schema.
                    "required": bool(field.get("required")) or bool(
                        projected and projected.rstrip().endswith("*")),
                    "recorded_semantic": audit.get("semantic"),
                    "recorded_status": audit.get("status"),
                })
    return rows


def as_recorded(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Score the verdicts a run actually reached, without re-deciding them.

    Replaying is right for a deterministic planner and wrong for a model: the synthetic
    schema a replay builds is not byte-identical to the live one, so every form would be a
    fresh call — paying again to re-derive an answer the run already recorded, and possibly
    getting a different one. What a run did is in its audit; this reads it.
    """
    resolved = [{**row,
                 "semantic": row["recorded_semantic"] or "unclassified",
                 "status": row["recorded_status"] or "deduplicated",
                 "planned": row["recorded_status"] == "planned"}
                for row in rows]
    return {"rows": resolved, "regressed": [], "reinterpreted": []}


def replay(rows: list[dict[str, Any]], classifier: Any) -> dict[str, Any]:
    """Re-plan every golden application through the real planner, not a re-implementation.

    `plan_for` is what the corpus run actually uses, so replaying through it picks up the
    guards and the group de-duplication too — a bench that re-derived "would this be
    planned?" from `semantic()` alone would have scored a refused password field as a win.
    The golden rows carry no `ref` (it is document-bound and meaningless later), so a
    synthetic one stands in: the planner only checks that a ref exists.

    A row that plans differently than it did at capture time is not automatically a
    problem — improving the ontology is the point, and every improvement disagrees with the
    record. What matters is the direction, so both are reported: a field that stopped being
    planned, and a field whose confident meaning changed. Broadening a pattern is how you
    accidentally answer a password box with a city name.
    """
    by_job: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_job[str(row["job_id"])].append(row)

    resolved: list[dict[str, Any]] = []
    for job, job_rows in by_job.items():
        schema = {"fields": [
            {"ref": f"g{i}", "label": r["label"], "name": r["name"], "kind": r["kind"],
             "group_label": r.get("group_label"),
             "options_sample": r.get("options_sample"), "required": r["required"]}
            for i, r in enumerate(job_rows)]}
        _plan, audit = classifier.plan_for(schema, job_rows[0]["language"])
        # `plan_for` skips duplicate unclassified radio groups, so audit is indexed by ref
        # rather than zipped positionally.
        by_ref = {a["ref"]: a for a in audit}
        for i, row in enumerate(job_rows):
            entry = by_ref.get(f"g{i}")
            resolved.append({
                **row, "job_id": job,
                # A row `plan_for` skipped is a de-duplicated radio option. Recompute its
                # meaning rather than carrying the recorded one forward: showing the old
                # verdict next to a new status reads as "still classified as city", which
                # is exactly the thing the change was meant to stop.
                "semantic": (entry or {}).get(
                    "semantic",
                    classifier.semantic({"label": row["label"], "name": row["name"],
                                         "kind": row["kind"],
                                         "group_label": row.get("group_label")})),
                "status": (entry or {}).get("status", "deduplicated"),
                "planned": bool(entry and entry["status"] == "planned"),
            })

    planned_groups = {(str(r["job_id"]), str(r["name"] or r["label"]))
                      for r in resolved if r["planned"]}
    regressed = [r for r in resolved
                 if r["recorded_status"] == "planned" and not r["planned"]
                 and (str(r["job_id"]), str(r["name"] or r["label"]))
                 not in planned_groups]
    reinterpreted = [
        r for r in resolved
        if r["recorded_semantic"] not in (r["semantic"], "unclassified")
        and r["semantic"] != "unclassified"]
    return {"rows": resolved, "regressed": regressed, "reinterpreted": reinterpreted}


def _decisions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per decision, not per control.

    A radio group is a single question however many options carry it, so counting its
    members individually inflates both the requirement and the shortfall — and it moves
    the score whenever the planner changes how many members it writes to, which is not a
    change in how much a person has to answer.
    """
    seen: set[tuple[str, str]] = set()
    out = []
    for row in rows:
        if row["kind"] in ("radio", "checkbox"):
            key = (str(row["job_id"]), str(row["name"] or row["label"]))
            if key in seen:
                continue
            seen.add(key)
        out.append(row)
    return out


def score(resolved: list[dict[str, Any]]) -> dict[str, Any]:
    required = _decisions([r for r in resolved if r["required"]])
    # A group is answered if any member is; the planner writes to one of them.
    planned_groups = {(str(r["job_id"]), str(r["name"] or r["label"]))
                      for r in resolved if r["planned"]}
    forced = [r for r in required if not r["planned"]
              and (str(r["job_id"]), str(r["name"] or r["label"])) not in planned_groups]
    per_job: dict[str, int] = defaultdict(int)
    for row in forced:
        per_job[str(row["job_id"])] += 1
    jobs = {str(r["job_id"]) for r in resolved}
    # Prefer the planner's own verdict to a regex over the label. `missing_profile` on a
    # `tailored_response` means the question was understood and has no supported answer —
    # which is exactly what the regex was trying to detect, only stated by the code that
    # knows. The regex stays as a fallback for what the ontology has not learned yet.
    open_ended = [r for r in forced
                  if (r["status"] == "missing_profile"
                      and r["semantic"] == "tailored_response")
                  or OPEN_ENDED.search(str(r["label"] or ""))]
    refused = [r for r in forced if r["status"] == "credential_refused"]
    # A rating matrix and an essay are the same kind of residue: understood, unanswerable
    # from a profile. Counting them as ontology gaps would make the gap look larger than it
    # is and would never close, since no pattern can answer "Python *".
    judged = [r for r in forced
              if r["status"] == "needs_judgement" and r not in open_ended]
    fixable = [r for r in forced
               if r not in open_ended and r not in refused and r not in judged]
    return {
        "fields": len(resolved),
        "required": len(required),
        "forced_decisions": len(forced),
        "forced_open_ended": len(open_ended),
        "forced_credential": len(refused),
        "forced_judgement": len(judged),
        "forced_fixable": len(fixable),
        "coverage": round(1 - len(forced) / len(required), 4) if required else 0.0,
        "applications": len(jobs),
        "forced_per_application": round(len(forced) / len(jobs), 2) if jobs else 0.0,
        "label_projected": sum(1 for r in resolved if r.get("label_projected")),
        "status_counts": Counter(r["status"] for r in resolved).most_common(),
        "forced_by_language": Counter(str(r["language"]) for r in forced).most_common(),
        "forced_by_kind": Counter(str(r["kind"]) for r in forced).most_common(),
        "worst_applications": Counter(per_job).most_common(5),
    }


def render(now: dict[str, Any], was: dict[str, Any] | None) -> list[str]:
    def delta(key: str, fmt: str = "{:+d}") -> str:
        if not was or key not in was:
            return ""
        d = now[key] - was[key]
        return "   ." if not d else "  " + fmt.format(d)

    out = [
        f"{now['fields']:,} fields over {now['applications']} applications with a form",
        f"{now.get('label_projected', 0):,} labels projected from a placeholder option",
        (f"{now['required']:,} required · {now['forced_decisions']:,} forced decisions"
         f"{delta('forced_decisions')}"),
        f"  {now['forced_fixable']:,} the ontology could answer{delta('forced_fixable')}",
        (f"  {now['forced_open_ended']:,} genuinely open-ended — a model must answer these"
         f"{delta('forced_open_ended')}"),
        (f"  {now.get('forced_judgement', 0):,} a rating or essay — judgement, not a lookup"
         f"{delta('forced_judgement')}"),
        (f"  {now.get('forced_credential', 0):,} credential fields, refused by design"
         f"{delta('forced_credential')}"),
        "",
        (f"coverage of required fields   {now['coverage']:.1%}"
         f"{delta('coverage', '{:+.1%}')}"),
        (f"forced decisions/application  {now['forced_per_application']:.2f}"
         f"{delta('forced_per_application', '{:+.2f}')}"),
        "",
        "forced by language: " + ", ".join(f"{k}={n}" for k, n in now["forced_by_language"]),
        "forced by kind:     " + ", ".join(f"{k}={n}" for k, n in now["forced_by_kind"]),
    ]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--golden", type=Path, nargs="*", default=[DEFAULT_GOLDEN])
    ap.add_argument("--out", type=Path,
                    default=ROOT / "outputs" / "classify" / "run.json")
    ap.add_argument("--baseline", type=Path, default=None)
    ap.add_argument("--as-recorded", action="store_true",
                    help="score the run's own verdicts instead of re-deciding them")
    ap.add_argument("--classifier", type=Path, default=None,
                    help="score with another build of the planner (see load_classifier)")
    ap.add_argument("--show-forced", type=int, default=0,
                    help="print N forced-decision labels the ontology could answer")
    args = ap.parse_args()

    rows = golden_rows([Path(p) for p in args.golden])
    if not rows:
        print("no field_audit rows in the golden set", file=sys.stderr)
        return 1
    played = (as_recorded(rows) if args.as_recorded
              else replay(rows, load_classifier(args.classifier)))
    for row in played["regressed"]:
        print(f"REGRESSION: {str(row['label'])[:50]!r} was planned as "
              f"{row['recorded_semantic']}, now {row['semantic']}/{row['status']}",
              file=sys.stderr)
    for row in played["reinterpreted"]:
        print(f"re-interpreted: {str(row['label'])[:50]!r} "
              f"{row['recorded_semantic']} -> {row['semantic']}", file=sys.stderr)

    report = score(played["rows"])
    report["regressed"] = len(played["regressed"])
    report["reinterpreted"] = len(played["reinterpreted"])
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    baseline = json.loads(args.baseline.read_text()) if args.baseline else None
    for line in render(report, baseline):
        print(line)
    if args.show_forced:
        print("\nforced decisions the ontology could answer:")
        seen: set[str] = set()
        for row in played["rows"]:
            if not row["required"] or row["planned"]:
                continue
            label = str(row["label"] or "")
            if OPEN_ENDED.search(label) or label in seen:
                continue
            seen.add(label)
            print(f"  [{row['language']:<5}] {label[:56]:<58} | {row['kind']}")
            if len(seen) >= args.show_forced:
                break
    print(f"\n→ {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
