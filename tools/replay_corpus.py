"""Second application at the same employer: discover once, replay the rest.

Runs inside `bh`, reusing the collector's profile/planner setup:

    BH_APPLICATION_INPUT=jobs_run100.json BH_APPLICATION_TELEMETRY_OUT=out/replay-1 \
      uv run bh < tools/replay_corpus.py

`BH_REPLAY` selects the mode:

* ``auto`` (default) — a posting whose employer has a live recording is replayed; a replay
  that does not verify falls back to discovery; every successful discovery records. This is
  the transparent cache: nothing to configure, a new site just works and is remembered.
* ``record`` — discover everything, record everything, never replay (a warm-up pass).
* ``0`` — discover everything, record nothing (the baseline).

Scheduling (2026-08-29): an employer's *first* posting runs in phase 1 (one per employer,
in parallel); every further posting runs in phase 2 as its own item, so seven ti&m
postings replay side by side instead of queueing on one worker — the critical path of a
run is one discovery plus one replay, not the longest employer chain.

Workers are tabs in one window. Hosts whose application page renders blank while hidden
are learnt (`experiments/hidden_hosts.json`) and get a tiled visible window; hosts that
ended on an account wall are learnt too (`experiments/wall_hosts.json`) and skipped.

Recordings: `BH_REPLAY_DIR/<host>.json` (see `applications.replay.RecordingStore`).
"""
# ruff: noqa: F821
from __future__ import annotations

import contextlib
import itertools
import json
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

# --- reuse the collector's setup (profile, ontology, planner, one_job) without running it
_ROOT = Path.cwd()
_src = (_ROOT / "tools" / "collect_job_form_telemetry.py").read_text(encoding="utf-8")
_g = globals()
_saved_name = _g.get("__name__")
_g["__name__"] = "replay_corpus"
exec(compile(_src, str(_ROOT / "tools" / "collect_job_form_telemetry.py"), "exec"), _g)  # noqa: S102
_g["__name__"] = _saved_name

from applications.replay import RecordingStore, host_of, record_from_result, replay_application

MODE = os.environ.get("BH_REPLAY", "auto").strip().lower()
if MODE in ("1", "true", "yes", "on"):
    MODE = "auto"
if MODE in ("false", "no", "off"):
    MODE = "0"
REPLAY_ON = MODE == "auto"
RECORD_ON = MODE in ("auto", "record")
STORE = RecordingStore(os.environ.get("BH_REPLAY_DIR", str(_ROOT.parent / "experiments" / "replay")))
SPLIT_CHAINS = os.environ.get("BH_REPLAY_SPLIT", "1").strip() != "0"


def start_url_of(job: dict[str, Any]) -> str:
    return (job.get("apply") or {}).get("direct_url") or job.get("url") or ""


# --- learnt host lists: visible window for hidden-blank hosts, skip for account walls ----
def _load_hosts(path: Path) -> set[str]:
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


HIDDEN_HOSTS_PATH = Path(os.environ.get("BH_HIDDEN_HOSTS", str(_ROOT.parent / "experiments" / "hidden_hosts.json")))
WALL_HOSTS_PATH = Path(os.environ.get("BH_WALL_HOSTS", str(_ROOT.parent / "experiments" / "wall_hosts.json")))
HIDDEN_HOSTS = _load_hosts(HIDDEN_HOSTS_PATH)
WALL_HOSTS = _load_hosts(WALL_HOSTS_PATH)
_hosts_lock = threading.Lock()
_window_slots = itertools.count()
WINDOW_SLOTS = int(os.environ.get("BH_WINDOW_SLOTS", "12"))


def remember_host(hosts: set[str], path: Path, host: str) -> None:
    with _hosts_lock:
        if host in hosts:
            return
        hosts.add(host)
        with contextlib.suppress(OSError):
            path.write_text(json.dumps(sorted(hosts), indent=1), encoding="utf-8")


def in_window(fn):
    """Run `fn()` on a fresh, tiled, visible window tab; return to the worker's tab after."""
    worker_tab = session.tab().target_id
    win = session.new_tab("about:blank", new_window=True)
    try:
        try:
            session.place_window(win.target_id, slot=next(_window_slots) % WINDOW_SLOTS, slots=WINDOW_SLOTS)
        except Exception as error:  # noqa: BLE001 — placement is best effort
            session.journal.write("note", event="window_place_failed", error=str(error)[:120])
        session.use_tab(win.target_id)
        return fn()
    finally:
        with contextlib.suppress(Exception):
            session.use_tab(worker_tab)
        with contextlib.suppress(Exception):
            session.close_tab(win.target_id, wait=False)


def discover(job: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    with session.journal.bind(mode="discover"):
        value = one_job(job)
    entry.update({"status": value.get("status"), "ok": value.get("status") == "form_processed",
                  "discover_ms": value.get("wall_ms"), "fill_plan_count": value.get("fill_plan_count"),
                  "hops": len(value.get("hops") or [])})
    if RECORD_ON:
        rec = record_from_result(value)
        if rec:
            STORE.add(rec)
            entry["recorded"] = rec["fingerprint"]
        elif entry["ok"]:
            entry["not_recordable"] = True
    return value


def discover_where_it_renders(job: dict[str, Any], entry: dict[str, Any], host: str) -> None:
    if host in HIDDEN_HOSTS:
        entry["window"] = "known"
        in_window(lambda: discover(job, entry))
        return
    discover(job, entry)
    if entry.get("status") == "no_application_form":
        entry["window"] = "retry"
        in_window(lambda: discover(job, entry))
        if entry.get("ok"):
            remember_host(HIDDEN_HOSTS, HIDDEN_HOSTS_PATH, host)
            entry["window"] = "learnt"
    elif entry.get("status") == "authentication_required":
        remember_host(WALL_HOSTS, WALL_HOSTS_PATH, host)


def do_posting(job: dict[str, Any], position: int) -> dict[str, Any]:
    url = start_url_of(job)
    host = host_of(url)
    entry: dict[str, Any] = {"job_id": job["job_id"], "company": job.get("company"), "host": host,
                             "position": position, "url": url, "mode": "discover", "wall_ms": 0.0}
    attempt, reason = should_attempt(job)
    if attempt and host in WALL_HOSTS:
        attempt, reason = False, "learnt_account_wall"
    if not attempt:
        entry.update({"mode": "skipped", "reason": reason, "ok": False})
        return entry
    t0 = time.perf_counter()
    with session.journal.bind(item_id=job["job_id"], replay_position=position):
        candidates = STORE.candidates(host) if REPLAY_ON else []
        # A shared host (the Abacus jobportal serves BDO, Medgate, AKB, ...) holds one
        # recording per employer form: the one recorded for this employer goes first.
        company = (job.get("company") or "").strip().lower()
        candidates.sort(key=lambda r: 0 if (r.get("company") or "").strip().lower() == company else 1)
        replays = []
        replayed_ok = False
        needs_input = None
        for rec in candidates[:2]:
            def run_replay(rec=rec):
                return replay_application(session, url, rec, timeout=LOCATE_TIMEOUT,
                                          applicant=APPLICANT, profile=PROFILE)
            # The same window rule as discovery: a host that renders blank while hidden
            # gets a visible window; a replay that finds neither its apply control nor
            # its fields in a hidden tab is retried once in a window, and the host learnt.
            if host in HIDDEN_HOSTS:
                entry["window"] = "known"
                result = in_window(run_replay)
            else:
                result = run_replay()
                reason = str(result.get("reason") or "")
                if not result.get("ok") and result.get("stage") != "preflight" and (
                        reason.startswith("apply control not found") or " of " in reason and "not found" in reason):
                    entry["window"] = "retry"
                    result = in_window(run_replay)
                    if result.get("ok"):
                        remember_host(HIDDEN_HOSTS, HIDDEN_HOSTS_PATH, host)
                        entry["window"] = "learnt"
            replays.append({k: result.get(k) for k in ("ok", "stage", "reason", "planned", "filled", "partial",
                                                      "unfilled", "ms", "stages", "recording", "changed", "healed",
                                                      "healed_by", "navigated", "missing_required")})
            if result.get("stage") == "preflight":
                # An early return, before any navigation: this applicant cannot answer
                # required fields of the recorded form. Not a recording failure.
                needs_input = result.get("missing_required") or []
                break
            STORE.note(rec, bool(result.get("ok")))
            if result.get("ok"):
                replayed_ok = True
                break
            if result.get("stage") in ("wall", "step:click") and "timeout" in str(result.get("reason") or ""):
                break                       # the site, not the recording: a second candidate will not help
        if replays:
            entry["replays"] = replays
        if needs_input is not None:
            entry.update({"mode": "needs_input", "ok": False, "missing_required": needs_input})
        elif replayed_ok:
            entry.update({"mode": "replay", "ok": True, "replay_ms": replays[-1]["ms"],
                          "partial": bool(replays[-1].get("partial"))})
            # Who is actually in the form? A recording made as one persona must fill
            # the current one; both fixture personas are checked so a mix-up is visible.
            try:
                values = " ".join(str((v or {}).get("value") or "") for v in
                                  (session.tab().form_values().get("values") or []))
                seen: dict[str, bool] = {}
                expected_persona = None
                for p in (_ROOT / "tests" / "fixtures" / "personas").glob("*.json"):
                    who = json.loads(p.read_text(encoding="utf-8"))
                    seen[p.stem] = any(tok in values for tok in (who.get("first_name"), who.get("email")) if tok)
                    if who.get("first_name") == PROFILE.get("first_name"):
                        expected_persona = p.stem
                entry["persona_check"] = {
                    "expected": expected_persona or PROFILE.get("first_name"), "seen": seen,
                    "clean": (expected_persona is None or seen.get(expected_persona, False))
                             and not any(v for k, v in seen.items() if k != expected_persona)}
            except Exception as error:  # noqa: BLE001 — a failed read-back is data, not a crash
                entry["persona_check"] = {"error": f"{type(error).__name__}: {str(error)[:80]}"}
        else:
            entry["mode"] = "replay_fallback" if replays else "discover"
            discover_where_it_renders(job, entry, host)
    entry["wall_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    # One line per posting as it lands, so a parallel run is never silent.
    print(f"  {'OK ' if entry.get('ok') else '   '}{(entry.get('company') or '?')[:24]:<24} "
          f"{entry['mode']:<15} {str(entry.get('status') or entry.get('reason') or '')[:22]:<22} "
          f"{entry['wall_ms'] / 1000:6.1f}s", flush=True)
    return entry


def do_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A phase item: one employer's postings in order (phase 1 gets one, phase 2 gets one each)."""
    return [do_posting(job, job.get("_position", index)) for index, job in enumerate(items)]


def replay_main() -> None:
    jobs = json.loads(INPUT.read_text(encoding="utf-8"))["jobs"]
    groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for job in jobs:
        groups.setdefault(job.get("company") or host_of(start_url_of(job)), []).append(job)
    for items in groups.values():
        for index, job in enumerate(items):
            job["_position"] = index
    OUT.mkdir(parents=True, exist_ok=True)
    started = time.time()
    if SPLIT_CHAINS:
        phases = [[items[:1] for items in groups.values()],
                  [[job] for items in groups.values() for job in items[1:]]]
    else:
        phases = [list(groups.values())]
    records: list[dict[str, Any]] = []
    phase_walls = []
    for number, phase in enumerate(phases, 1):
        if not phase:
            continue
        t_phase = time.time()
        print(f"phase {number}: {len(phase)} items", flush=True)
        records += parallel(phase, do_items, workers=WORKERS, worker_limit=WORKER_LIMIT,
                            reuse_tabs=True, isolated=False, timeout=RUN_TIMEOUT,
                            item_id=lambda items: f"{items[0].get('company') or '?'}#{items[0].get('_position', 0)}")
        phase_walls.append(round(time.time() - t_phase, 1))
    entries = [e for r in records if r.get("ok") for e in (r.get("value") or [])]
    failed_groups = [{"item": [j.get("job_id") for j in (r.get("item") or [])], "class": r.get("class"), "error": r.get("error")}
                     for r in records if not r.get("ok")]
    modes: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        modes.setdefault(e["mode"], []).append(e)
    summary = {"wall_ms": round((time.time() - started) * 1000, 1), "phase_walls_s": phase_walls,
               "employers": len(groups), "postings": len(entries), "failed_groups": len(failed_groups), "mode": MODE,
               "by_mode": {m: {"n": len(v), "ok": sum(1 for e in v if e.get("ok")),
                               "median_wall_ms": sorted(float(e.get("wall_ms") or 0) for e in v)[len(v) // 2] if v else None}
                           for m, v in modes.items()},
               "recordings": STORE.count(), "hidden_hosts": sorted(HIDDEN_HOSTS), "wall_hosts": sorted(WALL_HOSTS)}
    (OUT / "replay_results.json").write_text(json.dumps(
        {"summary": summary, "entries": entries, "failed_groups": failed_groups}, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    print(json.dumps(summary, default=str))


replay_main()
