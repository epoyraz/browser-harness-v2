"""Item 30: the measurements, asserted (run manually: `.venv/bin/python tests/live/perf_check.py`).

Ports the §1 numbers that are the harness's to assert — primitive latencies, screenshot
variants, batch-vs-sequential — from remembered prose into checks that fail. Runs against
a scratch-profile Chrome; timings assert generous ceilings (a busy laptop must not flake
them) and print the actual medians for the record. The model-side numbers from §1 (the
65× agent-loop spread) are decisions, not harness behaviour, and stay out of scope here.

Also renders a live `--trace` of the run's journal at the end: fill_form's round-trip
count on the line is TODO 26's done-when, demonstrated on real traffic.
"""
from __future__ import annotations

import shutil
import statistics
import sys
import tempfile
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _browser

from evidence.trace import render
from harness.connect.cdp import Connection, WebSocketTransport
from harness.connect.endpoint import discover
from harness.connect.session import SessionRegistry
from harness.core.journal import Journal
from harness.ops.batch import fetch_all
from harness.ops.forms import fill_form, form_schema
from harness.ops.page import Tab

#: See check.py — Windows occlusion throttling drops Input.dispatchMouseEvent.
FIXTURES = ROOT / "tests" / "fixtures"

results: list[tuple[str, bool, str]] = []


class _Site(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/doc/"):
            body = b'{"ok": 1}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

    def log_message(self, *a):
        pass


def check(name: str, ok: bool, note: str = "") -> None:
    results.append((name, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<52} {note}")


def median_ms(fn, n=30) -> float:
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times)


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="bh-perf-"))
    site = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Site, directory=str(FIXTURES)))
    threading.Thread(target=site.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{site.server_port}"

    _browser.launch(scratch, window="1200,900")
    try:
        time.sleep(0.3)

        journal_path = scratch / "session.jsonl"
        r = discover({"BH_PROFILE_DIRS": str(scratch)})
        conn = Connection(WebSocketTransport(r.ws_url),
                          journal=Journal(journal_path, session="perf")).start()
        registry = SessionRegistry(conn)
        registry.discover()
        tid = next(t["targetId"] for t in
                   conn.request("Target.getTargets")["targetInfos"] if t["type"] == "page")
        tab = Tab(conn, registry, tid)
        tab.goto(f"{base}/abacus.html")

        # --- primitive latencies --------------------------------------------
        js_med = median_ms(lambda: tab.js("1+1"))
        check("js('1+1') median < 20ms", js_med < 20, f"{js_med:.1f}ms")
        cdp_med = median_ms(lambda: tab.cdp("Page.getLayoutMetrics"))
        check("cdp(getLayoutMetrics) median < 15ms", cdp_med < 15, f"{cdp_med:.1f}ms")

        # --- screenshot variants (item 21's D4 table, asserted) --------------
        tab.capture_screenshot(scratch / "warm.jpeg")          # raster warm-up, discarded
        t0 = time.perf_counter()
        jpeg = tab.capture_screenshot(scratch / "a.jpeg")
        jpeg_ms = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        small = tab.capture_screenshot(scratch / "b.jpeg", max_dim=800)
        small_ms = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        png = tab.capture_screenshot(scratch / "c.png")
        png_ms = (time.perf_counter() - t0) * 1000
        check("jpeg-css < png bytes (the default is the cheap one)",
              jpeg["bytes"] < png["bytes"],
              f"jpeg={jpeg['bytes']}B/{jpeg_ms:.0f}ms png={png['bytes']}B/{png_ms:.0f}ms")
        check("max_dim=800 smallest of the three",
              small["bytes"] < jpeg["bytes"],
              f"max800={small['bytes']}B/{small_ms:.0f}ms")
        check("warm jpeg < 400ms", jpeg_ms < 400, f"{jpeg_ms:.0f}ms")

        # --- batch vs sequential (D15, asserted) ------------------------------
        schema = form_schema(tab)
        text_fields = [f for f in schema["fields"]
                       if f["kind"] in ("text", "email", "tel", "textarea")][:6]
        plan = [{"ref": f["ref"], "value": f"v-{i}"} for i, f in enumerate(text_fields)]

        t0 = time.perf_counter()
        for step in plan:                                       # v1's shape: one per field
            assert fill_form(tab, [step]).ok
        seq_ms = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        assert fill_form(tab, plan).ok                          # v2's shape: one write
        batch_ms = (time.perf_counter() - t0) * 1000
        check("batch fill beats per-field fill", batch_ms < seq_ms,
              f"batch={batch_ms:.1f}ms vs sequential={seq_ms:.1f}ms "
              f"({seq_ms / max(batch_ms, 0.1):.1f}x)")

        # --- concurrent vs sequential fetch (D0, asserted) --------------------
        urls = [f"{base}/doc/{i}" for i in range(20)]
        t0 = time.perf_counter()
        assert fetch_all(tab, urls, concurrency=1).ok
        seq_fetch = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        assert fetch_all(tab, urls, concurrency=6).ok
        conc_fetch = (time.perf_counter() - t0) * 1000
        check("fetch_all conc=6 beats conc=1", conc_fetch < seq_fetch,
              f"conc6={conc_fetch:.0f}ms vs conc1={seq_fetch:.0f}ms "
              f"({seq_fetch / max(conc_fetch, 0.1):.1f}x)")

        conn.close()

        # --- TODO 26 on real traffic: the trace names where round trips went --
        lines = render(Journal(journal_path).entries())
        # The count now lands on fill_form itself: routing the machinery through the
        # isolated world removed the nested anonymous `js` span, so the span that names
        # the operation is also the span that carries its cost.
        fill_lines = [ln for ln in lines if ln.lstrip().startswith("fill_form")]
        print("\n  --- bh trace (excerpt) ---")
        for ln in lines[:6]:
            print(f"  {ln}")
        check("trace: the batched fill's cost is on the fill_form span",
              fill_lines and all(("cdp=1" in ln or "cdp=2" in ln) for ln in fill_lines),
              f"{len(fill_lines)} spans: " + (fill_lines[0].split("|")[0][:52].strip()
                                              if fill_lines else "none"))

    finally:
        _browser.kill(scratch)
        site.shutdown()
        shutil.rmtree(scratch, ignore_errors=True)

    failed = [n for n, ok_, _ in results if not ok_]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed"
          + (f" — FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
