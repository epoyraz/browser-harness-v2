"""A/B bench for the five telemetry-driven changes.

Every number printed here is wall-clock against a real Chrome and real fixtures. Run it
once before touching the harness and once after; `--label` names the run and the results
are appended to `outputs/improve_bench.jsonl` so before/after sit side by side.

    python tests/live/improve_bench.py --label before
    python tests/live/improve_bench.py --label after

The five theories, and what would falsify each:

  1. `goto` partial outcome    — hangload.html must return a usable page in << timeout.
                                 Falsified if the saving is small or the page is unusable.
  2. `apply_control`           — collapsed.html must yield a control when apply_link is null.
                                 Falsified if the button cannot be found or clicking it
                                 does not reveal the form.
  3. `frames()` fixed waits    — prepare_application on a frameless page must drop well
                                 below its measured 1225ms p50. Falsified if the cost was
                                 not the announcement waits after all.
  4. `wait_for_form`           — must return quickly and truthfully on a page with no form,
                                 where `wait_for` burns its whole timeout. Falsified if it
                                 is no faster or reports the wrong answer.
  5. `invisible_because`       — collapsed.html's verdict must attribute the hidden controls
                                 to a cause. Falsified if the histogram is empty or wrong.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _browser

from harness.connect.cdp import Connection, WebSocketTransport
from harness.connect.endpoint import discover
from harness.connect.session import SessionRegistry
from harness.core.journal import Journal
from harness.core.outcome import HarnessError
from harness.ops import forms
from harness.ops.page import Tab

FIXTURES = ROOT / "tests" / "fixtures"
OUT = ROOT / "outputs"


class _Site(BaseHTTPRequestHandler):
    """Serves the fixtures, plus `/hang`: accepted, headers sent, body never written.

    Deliberately NOT `SimpleHTTPRequestHandler`. With the stock file handler, Chrome never
    finished parsing the document at all — `readyState` stuck at `loading` with zero
    controls — so the fixture reproduced a blank page rather than the case it was written
    for. Reading the file and writing it with an explicit Content-Length reproduces the
    real shape: DOM parsed, form present, `load` never fires.
    """

    def do_GET(self):
        if self.path.split("?")[0] == "/delayed-data":
            # Long enough to outlive the adaptive minimum. The SPA shell must not be
            # returned while this content-producing Fetch is still in flight.
            time.sleep(1.8)
            body = json.dumps({"text": "Complete application data from the API."}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # Exact match, not a prefix: `/hangload.html` also starts with `/hang`, so a
        # prefix test tarpitted the DOCUMENT rather than its image and the fixture
        # reproduced a page that never parsed instead of one that never fires `load`.
        if self.path.split("?")[0] == "/hang":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", "9999999")     # promised, never delivered
            self.end_headers()
            while not getattr(self.server, "_stopping", False):
                time.sleep(0.2)
            return
        name = self.path.lstrip("/").split("?")[0]
        path = FIXTURES / name
        if not name or not path.is_file() or path.parent != FIXTURES:
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


def timed(fn):
    t0 = time.perf_counter()
    try:
        value, err = fn(), None
    except Exception as e:                                        # noqa: BLE001
        value, err = None, f"{type(e).__name__}: {str(e)[:90]}"
    return round((time.perf_counter() - t0) * 1000, 1), value, err


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    args = ap.parse_args()

    scratch = Path(tempfile.mkdtemp(prefix="bh-bench-"))
    site = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    site.daemon_threads = True          # /hang holds a thread until the process exits
    threading.Thread(target=site.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{site.server_port}"
    res: dict[str, object] = {"label": args.label}

    _browser.launch(scratch, window="1200,900")
    try:
        time.sleep(0.3)
        r = discover({"BH_PROFILE_DIRS": str(scratch)})
        conn = Connection(WebSocketTransport(r.ws_url),
                          journal=Journal(scratch / "s.jsonl", session="bench")).start()
        registry = SessionRegistry(conn)
        registry.discover()
        tid = next(t["targetId"] for t in
                   conn.request("Target.getTargets")["targetInfos"] if t["type"] == "page")
        tab = Tab(conn, registry, tid)

        print(f"=== {args.label} ===")

        # -- 1. goto against a page whose `load` never fires -------------------
        ms, val, err = timed(lambda: tab.goto(f"{base}/hangload.html", timeout=8.0))
        # Whether it raised or not, ask the page the question that actually matters: is
        # there a usable document here? A saving that hands back a blank page is not a
        # saving, it is a new bug — so the controls count is part of the pass condition.
        try:
            probe = tab.js("[document.readyState,"
                           " document.querySelectorAll('input,textarea').length]")
        except HarnessError:
            probe = ["unreachable", -1]
        res["goto_hang_ms"] = ms
        res["goto_hang_raised"] = err
        res["goto_hang_readystate"] = probe[0]
        res["goto_hang_controls"] = probe[1]
        res["goto_hang_result"] = val
        print(f"  1. goto(hangload, timeout=8) {ms:>8.1f}ms  raised={err}")
        print(f"     returned  : {val}")
        print(f"     page after: readyState={probe[0]!r} controls={probe[1]}")

        # -- 3. prepare_application's fixed cost on a frameless page -----------
        tab.goto(f"{base}/abacus.html", timeout=10.0)
        frame_ms = [timed(lambda: tab.frames())[0] for _ in range(3)]
        res["frames_ms"] = frame_ms
        print(f"  3. frames() x3 on a frameless page  {frame_ms} ms")

        # -- 1b. adaptive grace and the strict SPA invariant -------------------
        tab.goto(f"{base}/noform.html", timeout=10.0)
        grace, samples = tab._adaptive_navigation_grace(3.0)
        res["adaptive_navigation_grace_ms"] = round(grace * 1000, 1)
        res["adaptive_navigation_samples"] = samples
        res["adaptive_navigation_saved_ms"] = round((3.0 - grace) * 1000, 1)

        # A networkAlmostIdle signal is not sufficient when the remaining request is the
        # SPA's data. The result must contain the delayed payload, not merely its header.
        spa_ms, spa, spa_err = timed(lambda: tab.open_page(
            f"{base}/delayed-data-spa.html", timeout=5.0))
        spa_page = (spa or {}).get("page") or {}
        spa_text = str(spa_page.get("text") or "") + "\n" + "\n".join(
            str(block.get("text") or "") for block in spa_page.get("blocks") or []
            if isinstance(block, dict))
        spa_dom_complete = bool(tab.js(
            "document.getElementById('content')?.textContent.includes('Complete application data')"))
        res["adaptive_spa_ms"] = spa_ms
        res["adaptive_spa_error"] = spa_err
        res["adaptive_spa_complete"] = "Complete application data" in spa_text
        res["adaptive_spa_dom_complete"] = spa_dom_complete
        res["adaptive_spa_lifecycle"] = (spa or {}).get("lifecycle")
        print(f"  1b. delayed-data SPA                 {spa_ms:>8.1f}ms "
              f"complete={res['adaptive_spa_complete']} dom={spa_dom_complete} lifecycle="
              f"{res['adaptive_spa_lifecycle']!r}")

        # Exact mode must ignore the same settled pair and consume its whole deadline.
        strict_ms, strict, strict_err = timed(lambda: tab.goto(
            f"{base}/hangload.html", timeout=1.2, usable_after=None))
        res["strict_navigation_ms"] = strict_ms
        res["strict_navigation_error"] = strict_err
        res["strict_navigation_result"] = strict
        res["strict_navigation_unchanged"] = (
            strict_err is None and (strict or {}).get("lifecycle") == "timeout"
            and set(strict or {}) == {"requested", "landed", "lifecycle"}
            and strict_ms >= 1100)
        res["adaptive_navigation_pass"] = bool(
            grace < 3.0 and res["adaptive_spa_complete"]
            and res["strict_navigation_unchanged"])
        res["adaptive_navigation_failures"] = sum(
            value is not None for value in (spa_err, strict_err))
        res["adaptive_content_failures"] = int(not res["adaptive_spa_complete"])
        print(f"  1c. strict stalled navigation        {strict_ms:>8.1f}ms "
              f"unchanged={res['strict_navigation_unchanged']}")

        # -- 4. waiting, on the two pages where the blanket selector lies ------
        # (a) nothing to match: wait_for burns the whole timeout to learn "no".
        tab.goto(f"{base}/noform.html", timeout=10.0)
        ms, _, err = timed(lambda: tab.wait_for("input, textarea, form", state="visible",
                                                timeout=6.0))
        res["wait_for_noform_ms"], res["wait_for_noform_raised"] = ms, err
        print(f"  4a. wait_for  on a page with no form   {ms:>8.1f}ms raised={bool(err)}")
        if hasattr(tab, "wait_for_form"):
            ms, val, err = timed(lambda: tab.wait_for_form(timeout=6.0))
            res["wait_for_form_noform_ms"], res["wait_for_form_noform"] = ms, val
            print(f"      wait_for_form same page            {ms:>8.1f}ms -> {val}")

        # (b) furniture present, real form 1500ms away: wait_for returns instantly on the
        #     cookie checkbox and reports success for a page that has no form yet.
        tab.goto(f"{base}/lateform.html", timeout=10.0)
        ms, _, err = timed(lambda: tab.wait_for("input, textarea, form", state="visible",
                                                timeout=6.0))
        fields_then = len(forms.form_schema(tab).get("fields") or [])
        res["wait_for_late_ms"], res["wait_for_late_fields"] = ms, fields_then
        print(f"  4b. wait_for  on a late form           {ms:>8.1f}ms "
              f"-> {fields_then} real fields present (form arrives at 1500ms)")
        if hasattr(tab, "wait_for_form"):
            tab.goto(f"{base}/lateform.html", timeout=10.0)
            ms, val, err = timed(lambda: tab.wait_for_form(timeout=6.0))
            res["wait_for_form_late_ms"], res["wait_for_form_late"] = ms, val
            print(f"      wait_for_form same page            {ms:>8.1f}ms -> {val}")
        else:
            print("      wait_for_form                      not implemented")

        # -- 2 + 5. the collapsed form behind a button -------------------------
        tab.goto(f"{base}/collapsed.html", timeout=10.0)
        prep = forms.prepare_document(tab)
        verdict = (prep.get("schema") or {}).get("verdict") or {}
        res["collapsed_apply_link"] = prep.get("apply_link")
        res["collapsed_apply_control"] = prep.get("apply_control")
        res["collapsed_verdict"] = verdict
        print(f"  2. apply_link={prep.get('apply_link')}  "
              f"apply_control={prep.get('apply_control')}")
        print(f"  5. reason={verdict.get('reason')!r}")
        print(f"     invisible_because={verdict.get('invisible_because')}")

        # can we actually get through to the form?
        got_form = False
        ctrl = prep.get("apply_control")
        if isinstance(ctrl, dict) and ctrl.get("ref"):
            try:
                tab.click_ref(ctrl["ref"])
                after = forms.prepare_document(tab)
                got_form = bool(((after.get("schema") or {}).get("verdict") or {})
                                .get("is_form"))
            except HarnessError as e:
                res["collapsed_click_error"] = str(e)[:90]
        res["collapsed_reached_form"] = got_form
        print(f"  2b. reached the form after clicking: {got_form}")

        conn.close()
    finally:
        site._stopping = True
        _browser.kill(scratch)
        site.shutdown()
        shutil.rmtree(scratch, ignore_errors=True)

    OUT.mkdir(exist_ok=True)
    with (OUT / "improve_bench.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(res, default=str) + "\n")
    print(f"\nappended to {OUT / 'improve_bench.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
