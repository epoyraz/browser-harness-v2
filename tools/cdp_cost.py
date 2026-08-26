"""Measure CDP round trips per helper against local fixtures, repeatably.

The 100-job live corpus answers "what does a real run cost", but it is the wrong
instrument for iterating on round-trip counts: it takes ~150s, hits 100 third-party sites,
and its numbers move with employer-side latency. This runs the same helper paths against
`tests/fixtures/` on a scratch-profile Chrome, so a count that changes did so because the
code changed.

    uv run python tools/cdp_cost.py --rounds 3 --out outputs/cdp-cost/baseline.json
    uv run python tools/cdp_cost.py --rounds 3 --out outputs/cdp-cost/after.json \
        --baseline outputs/cdp-cost/baseline.json

The report is per helper and per CDP method, taken from the journal the run already
writes — the same source `bh stats` reads, so there is no second measurement path to
disagree with it.
"""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "live"))

FIXTURES = ROOT / "tests" / "fixtures"

#: The parent is served from 127.0.0.1 and the child from localhost, which Chrome treats
#: as different sites — so the child is a real out-of-process iframe and `frames()` takes
#: its non-zero branch rather than the shortcut this rig exists to measure.
IFRAMED = """<!doctype html><meta charset=utf-8><title>Posting</title>
<h1>Senior Engineer</h1><p>The application lives in the frame below.</p>
<iframe src="{child}" width="600" height="400"></iframe>"""


class _Site(BaseHTTPRequestHandler):
    def do_GET(self):
        name = self.path.lstrip("/").split("?")[0]
        if name == "iframed":
            body = IFRAMED.format(child=self.server.child_form).encode()
        else:
            path = FIXTURES / f"{name}.html"
            if not path.is_file():
                self.send_error(404)
                return
            body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


# -- the scenario ---------------------------------------------------------------


def _text_fields(prepared: dict[str, Any], limit: int = 4) -> list[dict[str, Any]]:
    """Fillable text-ish fields, by ref, so the scenario does not pin fixture markup."""
    fields = (prepared.get("schema") or {}).get("fields") or []
    out = []
    for field in fields:
        if field.get("kind") in {"text", "email", "tel", "textarea"} and field.get("ref"):
            out.append({"ref": field["ref"], "value": "Test"})
        if len(out) >= limit:
            break
    return out


def scenario(session: Any, base: str, home: str, steps: Counter) -> None:
    """One round of the paths the CDP-cost work touches.

    Deliberately ordered as a real attempt runs: land, classify, fill. Each step records
    that it actually happened, so a round that silently skipped one is visible in the
    report instead of reading as a saving.

    Every round starts by re-pinning ``home``. ``prepare_application`` moves the cursor to
    whichever context won — on the iframed posting that is the child target, which the next
    navigation destroys. Rounds must not inherit a dead cursor from their predecessor.
    """
    from harness.ops import forms
    from harness.ops import parallel as parallel_ops

    session.use_tab(home)
    tab = session.tab()

    # 1. an application form: prepare_application returns without ever calling frames()
    tab.goto(f"{base}/personio")
    prepared = session.prepare_application()
    steps["form.prepare"] += 1
    tab.wait_for_application_state()
    steps["form.state"] += 1
    if values := _text_fields(prepared):
        forms.fill_form(tab, values)
        steps["form.fill"] += 1

    # 2. no form at all: this is the round trip budget frames() spends on a zero
    tab.goto(f"{base}/noform")
    session.prepare_application()
    steps["noform.prepare"] += 1
    tab.wait_for_application_state()
    steps["noform.state"] += 1

    # 3. a posting whose application is inside a cross-origin frame: the other branch
    tab.goto(f"{base}/iframed")
    session.prepare_application()
    steps["iframed.prepare"] += 1

    # 4. an ARIA combobox
    tab.goto(f"{base}/combobox")
    schema = forms.form_schema(tab)
    combo = next((f for f in (schema.get("fields") or [])
                  if f.get("kind") == "combobox" and f.get("ref")), None)
    if combo is not None:
        forms.select_option(tab, combo["ref"], "LinkedIn")
        steps["combobox.select"] += 1

    # 5. parallel, for the per-item tab cleanup path
    def one(url: str) -> int:
        page = session.tab()
        page.goto(url)
        return len(page.snapshot())

    parallel_ops.parallel(
        session,
        [f"{base}/noform", f"{base}/personio", f"{base}/noform", f"{base}/personio"],
        one, workers=2, reuse_tabs=True,
    )
    steps["parallel.items"] += 4


# -- reading the journal back ---------------------------------------------------


def report(journal: Path) -> dict[str, Any]:
    """Per-helper and per-method round trips, attributed through the journal's parents."""
    from harness.core import jsonl

    entries = list(jsonl.read(journal))
    calls = {e["id"]: e for e in entries if e.get("kind") == "call" and e.get("id")}
    helpers: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "cdp": 0, "ms": 0.0, "methods": Counter()})
    for entry in entries:
        if entry.get("kind") != "call":
            continue
        stats = helpers[str(entry.get("fn") or "?")]
        stats["calls"] += 1
        stats["ms"] += float(entry.get("ms") or 0)
        # The span's own count, not a reconstruction: `cdp` is billed to the innermost
        # open span at request time, which is what `bh stats` reports.
        stats["cdp"] += int(entry.get("cdp") or 0)

    methods: Counter[str] = Counter()
    method_ms: dict[str, float] = defaultdict(float)
    unattributed: Counter[str] = Counter()
    for entry in entries:
        if entry.get("kind") != "cdp":
            continue
        method = str(entry.get("method"))
        methods[method] += 1
        method_ms[method] += float(entry.get("ms") or 0)
        parent = calls.get(entry.get("parent"))
        if parent is None:
            unattributed[method] += 1
            continue
        helpers[str(parent.get("fn") or "?")]["methods"][method] += 1

    return {
        "cdp_total": sum(methods.values()),
        "helpers": {
            fn: {"calls": s["calls"], "cdp": s["cdp"],
                 "cdp_per_call": round(s["cdp"] / s["calls"], 2) if s["calls"] else 0.0,
                 "ms_per_call": round(s["ms"] / s["calls"], 1) if s["calls"] else 0.0,
                 "methods": dict(s["methods"].most_common())}
            for fn, s in sorted(helpers.items(), key=lambda kv: -kv[1]["cdp"])
        },
        "methods": dict(methods.most_common()),
        "method_ms": {m: round(ms, 1) for m, ms in sorted(
            method_ms.items(), key=lambda kv: -kv[1])},
        "unattributed": dict(unattributed.most_common()),
    }


def render(r: dict[str, Any], baseline: dict[str, Any] | None = None) -> list[str]:
    def delta(now: float, was: float | None) -> str:
        if was is None:
            return ""
        d = now - was
        return "        ." if abs(d) < 0.005 else f"{d:>+9.2f}"

    base_helpers = (baseline or {}).get("helpers") or {}
    base_methods = (baseline or {}).get("methods") or {}
    was_total = (baseline or {}).get("cdp_total")
    out = [f"CDP round trips: {r['cdp_total']:,}"
           + (f"   ({r['cdp_total'] - was_total:+,} vs baseline)" if was_total else "")]
    out.append("")
    out.append(f"{'helper':<28}{'calls':>7}{'cdp':>7}{'cdp/call':>10}{'Δ/call':>10}")
    for fn, s in r["helpers"].items():
        if not s["cdp"]:
            continue
        was = base_helpers.get(fn, {}).get("cdp_per_call")
        out.append(f"{fn[:28]:<28}{s['calls']:>7}{s['cdp']:>7}"
                   f"{s['cdp_per_call']:>10.2f}{delta(s['cdp_per_call'], was):>10}")
    out.append("")
    out.append(f"{'method':<40}{'n':>7}{'Δ':>9}{'ms':>10}")
    for method, n in r["methods"].items():
        was = base_methods.get(method)
        d = "" if was is None else ("        ." if n == was else f"{n - was:>+9}")
        out.append(f"{method[:40]:<40}{n:>7}{d:>9}{r['method_ms'].get(method, 0):>10.0f}")
    return out


# -- run ------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--out", type=Path, default=ROOT / "outputs" / "cdp-cost" / "run.json")
    ap.add_argument("--baseline", type=Path, default=None)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--keep-journal", action="store_true")
    args = ap.parse_args()

    import os

    import _browser

    scratch = Path(tempfile.mkdtemp(prefix="bh-cdp-cost-"))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    journal = args.out.with_suffix(".journal.jsonl")
    if journal.exists():
        journal.unlink()

    site = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    child = ThreadingHTTPServer(("localhost", 0), _Site)
    site.child_form = f"http://localhost:{child.server_port}/personio"
    child.child_form = ""
    for server in (site, child):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{site.server_port}"

    if not args.headed:
        os.environ.setdefault("BH_HEADLESS", "1")
    os.environ["BH_PROFILE_DIRS"] = str(scratch)
    os.environ["BH_JOURNAL"] = str(journal)
    # Per-method detail is opt-in; without it the journal carries only each span's
    # round-trip total, which cannot say *which* call a saving came from.
    os.environ["BH_CDP_TRACE"] = "1"

    _browser.launch(scratch, window="1200,900")
    steps: Counter[str] = Counter()
    wall: list[float] = []
    try:
        from harness.session import Session

        session = Session("cdpcost", journal_path=str(journal))
        home = session.tab().target_id
        for _ in range(args.rounds):
            started = time.perf_counter()
            scenario(session, base, home, steps)
            wall.append(round((time.perf_counter() - started) * 1000, 1))
    finally:
        _browser.kill(scratch)
        site.shutdown()
        child.shutdown()
        shutil.rmtree(scratch, ignore_errors=True)

    r = report(journal)
    r["rounds"] = args.rounds
    r["steps"] = dict(steps)
    r["round_wall_ms"] = wall
    r["round_wall_median_ms"] = round(statistics.median(wall), 1) if wall else 0.0
    args.out.write_text(json.dumps(r, indent=2), encoding="utf-8")
    if not args.keep_journal:
        journal.unlink(missing_ok=True)

    baseline = json.loads(args.baseline.read_text()) if args.baseline else None
    if baseline and baseline.get("steps") != r["steps"]:
        print(f"WARNING: step counts differ from baseline "
              f"({baseline.get('steps')} vs {r['steps']}) — the runs are not comparable",
              file=sys.stderr)
    for line in render(r, baseline):
        print(line)
    print(f"\nrounds={args.rounds}  median round {r['round_wall_median_ms']:.0f}ms  "
          f"→ {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
