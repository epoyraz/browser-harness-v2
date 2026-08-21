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
import statistics
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _browser

from harness.connect.cdp import WebSocketTransport
from harness.connect.daemon import Daemon
from harness.connect.endpoint import discover
from harness.core.journal import Journal
from harness.core.outcome import SideEffectRefused
from harness.ops.parallel import parallel, summarise
from harness.session import Session

#: Per-request delay. Long enough that a serial run cannot be mistaken for a parallel one,
#: short enough that the whole check stays under ~15s.
DELAY = 0.6
WORKERS = 6

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, note: str = "") -> None:
    results.append((name, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<52} {note}")


class _Site(BaseHTTPRequestHandler):
    active = 0
    peak = 0
    lock = threading.Lock()
    posts = 0

    def do_GET(self):
        with self.lock:
            type(self).active += 1
            type(self).peak = max(type(self).peak, type(self).active)
        try:
            time.sleep(DELAY)
            n = self.path.strip("/") or "0"
            form = ("<form id=application method=post action=/sent>"
                    "<input name=name value=test><button id=submit type=submit>Send</button>"
                    "</form>") if self.path.startswith("/safety") else ""
            body = (f"<!doctype html><title>page {n}</title>"
                    f"<body><h1 id=n>{n}</h1>{form}</body>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass                    # closing a worker tab may cancel its final read
        finally:
            with self.lock:
                type(self).active -= 1

    def do_POST(self):
        with self.lock:
            type(self).posts += 1
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass

    def handle_error(self, *a):
        pass        # Chrome dropping a connection at teardown is not an error worth a trace


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="bh-par-"))
    runtime = Path(tempfile.mkdtemp(prefix="bhp-"))
    # THREADING server, deliberately: a plain HTTPServer handles one request at a time,
    # which serialises the very thing under test. The first run of this check reported
    # parallel as 0.5x *slower* than serial purely because of that.
    site = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    site.daemon_threads = True
    threading.Thread(target=site.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{site.server_port}"

    os.environ.update({"BH_PROFILE_DIRS": str(scratch), "BH_RUNTIME_DIR": str(runtime),
                       "BU_CDP_URL": "", "BU_CDP_WS": "", "BH_RECORD": "0"})
    daemon = None
    session = None
    _browser.launch(scratch, window="1100,800")
    try:
        # Keep the daemon in this process so the check owns its entire lifecycle. The old
        # auto-spawned shape left a detached daemon behind after every run.
        resolution = discover({"BH_PROFILE_DIRS": str(scratch)})
        daemon = Daemon("parcheck", lambda: WebSocketTransport(resolution.ws_url)).start()
        threading.Thread(target=daemon.serve_forever, daemon=True).start()

        journal = scratch / "j.jsonl"
        session = Session("parcheck", journal_path=str(journal))
        urls = [f"{base}/{i}" for i in range(12)]
        baseline_targets = {t["targetId"] for t in session.targets()}
        namespace = session.namespace()
        wave = threading.Barrier(WORKERS, timeout=20)

        def visit(url: str) -> str:
            # Use the public bare-helper surface, not a Tab captured before the barrier.
            # With a process-wide cursor every js() below would read whichever tab navigated
            # last; the barrier makes that cross-routing deterministic rather than lucky.
            namespace["goto"](url)
            wave.wait()
            return namespace["js"]("document.querySelector('#n').textContent")

        def visit_serial(url: str) -> str:
            namespace["goto"](url)
            return namespace["js"]("document.querySelector('#n').textContent")

        def target_ids() -> set[str]:
            return {t["targetId"] for t in session.targets()}

        def tabs_returned_to_baseline(timeout: float = 3.0) -> bool:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if target_ids() == baseline_targets:
                    return True
                time.sleep(0.05)
            return target_ids() == baseline_targets

        # The popup-safety scan is one Target.getTargets round trip per item. Measure it
        # independently on 100 no-op jobs so its cost is visible rather than hidden inside
        # navigation timing.
        cleanup_probe = parallel(session, range(100), lambda _: None, workers=WORKERS)
        cleanup_ms = sorted(
            float(record["telemetry"]["cleanup_target_query_ms"])
            for record in cleanup_probe
            if record["telemetry"]["cleanup_target_query_ms"] is not None
        )
        check("100 popup-cleanup target queries are measured", len(cleanup_ms) == 100,
              (f"p50={statistics.median(cleanup_ms):.1f}ms "
               f"p95={cleanup_ms[94]:.1f}ms max={cleanup_ms[-1]:.1f}ms")
              if cleanup_ms else "no samples")
        check("cleanup probe returns worker tabs to baseline", tabs_returned_to_baseline(),
              f"baseline={len(baseline_targets)} now={len(target_ids())}")

        t0 = time.perf_counter()
        got = parallel(session, urls, visit, workers=WORKERS)
        par = time.perf_counter() - t0

        s = summarise(got)
        check("every item accounted for", s["total"] == 12, f"{s['total']}/12")
        check("all succeeded", s["failed"] == 0, f"{s['ok']} ok, {s['failed']} failed")
        check("results in input order",
              [r["value"] for r in got] == [str(i) for i in range(12)],
              str([r["value"] for r in got])[:46])

        cleanup_queries = [r["telemetry"]["cleanup_target_query_ms"] for r in got]
        cleanup_total = sum(value for value in cleanup_queries if value is not None)
        check("popup cleanup target queries are measured",
              all(value is not None for value in cleanup_queries),
              f"{cleanup_total:.1f}ms total / {len(cleanup_queries)} items")

        check("real requests overlapped", _Site.peak > 1, f"peak={_Site.peak}")
        check("worker tabs cleaned up", tabs_returned_to_baseline(),
              f"baseline={len(baseline_targets)} now={len(target_ids())}")

        peak_tabs = 0
        watching = threading.Event()

        def watch_tabs():
            nonlocal peak_tabs
            while not watching.is_set():
                peak_tabs = max(peak_tabs, len(target_ids() - baseline_targets))
                time.sleep(0.01)

        watcher = threading.Thread(target=watch_tabs, daemon=True)
        watcher.start()
        clean = parallel(session, urls, visit_serial, workers=3, reuse_tabs=False)
        watching.set()
        watcher.join(2)
        check("clean-tab mode succeeds", summarise(clean)["failed"] == 0)
        check("clean-tab mode never exceeds worker tab budget", peak_tabs <= 3,
              f"peak worker tabs={peak_tabs}")
        check("clean-tab mode closes every tab", tabs_returned_to_baseline(),
              f"baseline={len(baseline_targets)} now={len(target_ids())}")

        contexts = [session.new_context(), session.new_context()]
        context_tabs = [session.new_tab(context_id=context_id) for context_id in contexts]
        for index, tab in enumerate(context_tabs):
            tab.goto(f"{base}/context")
            tab.js(f"localStorage.setItem('worker', '{index}')")
        isolated_values = [tab.js("localStorage.getItem('worker')") for tab in context_tabs]
        check("browser contexts isolate local storage", isolated_values == ["0", "1"],
              str(isolated_values))
        for context_id in contexts:
            session.close_context(context_id)
        check("isolated contexts return targets to baseline", tabs_returned_to_baseline())

        safety = session.new_tab(f"{base}/safety")
        submit = next(element for element in safety.snapshot()
                      if element.get("name") == "Send")
        refused = False
        try:
            safety.click_ref(submit["ref"])
        except SideEffectRefused:
            refused = True
        # Read-only boot-time POSTs are deliberately allowed so modern ATS SPAs can
        # render. Once applicant data is present the form helpers arm this boundary;
        # model that state explicitly before testing mutating network calls.
        safety.arm_dry_run()
        fetch_blocked = safety.js(
            "await fetch('/sent', {method:'POST', body:'x'}).then(() => false, () => true)")
        safety.js("HTMLFormElement.prototype.submit.call(document.getElementById('application'))")
        time.sleep(0.2)
        check("submit click is refused before dispatch", refused)
        check("mutating fetch and form.submit are blocked", fetch_blocked and _Site.posts == 0,
              f"posts={_Site.posts}")
        session.close_tab(safety.target_id)
        check("safety tab cleaned up", tabs_returned_to_baseline())

        # A/B on the *same* workload. Comparing 12-parallel against 6-serial was the first
        # shape here and it is not a measurement: it mixes a throughput change with a
        # workload change, so the ratio means nothing in particular.
        t0 = time.perf_counter()
        for u in urls:
            visit_serial(u)
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
            return visit_serial(url)

        mixed = summarise(parallel(session, urls[:6], flaky, workers=3))
        check("one failure does not cancel siblings",
              mixed["ok"] == 5 and mixed["failed"] == 1, str(mixed["classes"]))

        # Spans must nest inside their own worker, not across workers.
        entries = [e for e in Journal(journal).entries() if e.get("kind") == "call"]
        ids = {e["id"] for e in entries}
        orphans = [e for e in entries if e.get("parent") and e["parent"] not in ids]
        check("no orphaned span parents", not orphans, f"{len(entries)} spans")

    finally:
        if session is not None:
            session.close()
        if daemon is not None:
            daemon.stop()
        _browser.kill(scratch)
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
