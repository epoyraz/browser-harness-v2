"""Live: `parallel()` against a real browser.

The unit tests prove the bookkeeping — order, isolation, failure capture — against a fake
session. What they cannot prove is the part that actually motivated this: that one
websocket, one daemon and N real tabs genuinely overlap, and that the per-thread cursor
survives contact with CDP rather than just with a dict.

So this measures. The site deliberately sleeps per request, which makes serial and
parallel unmistakably different: 12 pages x ~0.6s is ~7s one at a time and ~1s across 6
workers. If the speedup is not there, the concurrency is a lie somewhere below this file.

    .venv/bin/python tests/live/parallel_check.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from harness.core.journal import Journal
from harness.ops.parallel import parallel, summarise
from harness.session import Session

CHROME = (os.environ.get("BH_CHROME")
          or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

#: Per-request delay. Long enough that a serial run cannot be mistaken for a parallel one,
#: short enough that the whole check stays under ~15s.
DELAY = 0.6

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, note: str = "") -> None:
    results.append((name, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<52} {note}")


class _Site(BaseHTTPRequestHandler):
    def do_GET(self):
        time.sleep(DELAY)
        n = self.path.strip("/") or "0"
        body = (f"<!doctype html><title>page {n}</title>"
                f"<body><h1 id=n>{n}</h1></body>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def handle_error(self, *a):
        pass        # Chrome dropping a connection at teardown is not an error worth a trace


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="bh-par-"))
    runtime = Path(tempfile.mkdtemp(prefix="bhp-", dir="/tmp"))
    # THREADING server, deliberately: a plain HTTPServer handles one request at a time,
    # which serialises the very thing under test. The first run of this check reported
    # parallel as 0.5x *slower* than serial purely because of that.
    site = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    site.daemon_threads = True
    threading.Thread(target=site.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{site.server_port}"

    # Detached via `open`, so launchd owns Chrome rather than this terminal. Launching it
    # as our own child made macOS attribute Chrome's file access to the terminal app and
    # revoked its Desktop permission when the resulting prompt went unanswered.
    subprocess.run(
        ["/usr/bin/open", "-na", CHROME, "--args", f"--user-data-dir={scratch}",
         "--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check",
         f"--download-directory={scratch}", "--window-size=1100,800", "about:blank"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.environ.update({"BH_PROFILE_DIRS": str(scratch), "BH_RUNTIME_DIR": str(runtime),
                       "BU_CDP_URL": "", "BU_CDP_WS": "", "BH_RECORD": "0"})
    try:
        deadline = time.monotonic() + 25
        while not (scratch / "DevToolsActivePort").exists():
            if time.monotonic() > deadline:
                print("Chrome never wrote DevToolsActivePort")
                return 1
            time.sleep(0.1)
        time.sleep(0.4)

        journal = scratch / "j.jsonl"
        session = Session("parcheck", journal_path=str(journal))
        urls = [f"{base}/{i}" for i in range(12)]

        def visit(url: str) -> str:
            tab = session.tab()
            tab.goto(url)
            return tab.js("document.querySelector('#n').textContent")

        t0 = time.perf_counter()
        got = parallel(session, urls, visit, workers=6)
        par = time.perf_counter() - t0

        s = summarise(got)
        check("every item accounted for", s["total"] == 12, f"{s['total']}/12")
        check("all succeeded", s["failed"] == 0, f"{s['ok']} ok, {s['failed']} failed")
        check("results in input order",
              [r["value"] for r in got] == [str(i) for i in range(12)],
              str([r["value"] for r in got])[:46])

        # Checked HERE, before the serial loop below opens a tab of its own — otherwise
        # this counts that tab and reports a leak that is not one.
        left = [t for t in session.targets() if str(t.get("url", "")).startswith(base)]
        check("worker tabs cleaned up", not left, f"{len(left)} left open")

        # A/B on the *same* workload. Comparing 12-parallel against 6-serial was the first
        # shape here and it is not a measurement: it mixes a throughput change with a
        # workload change, so the ratio means nothing in particular.
        t0 = time.perf_counter()
        for u in urls:
            visit(u)
        serial = time.perf_counter() - t0
        speedup = serial / max(par, 0.001)
        check("parallel beats serial on identical work", par < serial,
              f"par={par:.1f}s  serial={serial:.1f}s")
        # 6 workers will not give 6x: Chrome throttles background tabs, and each worker
        # pays a one-off tab setup (createTarget, attach, isolated world). 2x is the bar
        # for "the overlap is real" without encoding today's constant factors.
        check("speedup is substantial", speedup >= 2.0, f"{speedup:.1f}x on 12 pages")

        # One failing page must not take its siblings with it.
        def flaky(url: str) -> str:
            if url.endswith("/3"):
                raise ValueError("boom")
            return visit(url)

        mixed = summarise(parallel(session, urls[:6], flaky, workers=3))
        check("one failure does not cancel siblings",
              mixed["ok"] == 5 and mixed["failed"] == 1, str(mixed["classes"]))

        # Spans must nest inside their own worker, not across workers.
        entries = [e for e in Journal(journal).entries() if e.get("kind") == "call"]
        ids = {e["id"] for e in entries}
        orphans = [e for e in entries if e.get("parent") and e["parent"] not in ids]
        check("no orphaned span parents", not orphans, f"{len(entries)} spans")

        session.close()
    finally:
        subprocess.run(["/usr/bin/pkill", "-f", f"--user-data-dir={scratch}"],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.8)
        site.shutdown()
        shutil.rmtree(scratch, ignore_errors=True)
        shutil.rmtree(runtime, ignore_errors=True)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
