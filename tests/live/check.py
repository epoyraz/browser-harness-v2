"""Live validation against real Chrome (run manually: `.venv/bin/python tests/live/check.py`).

Launches a scratch-profile Chrome — own --user-data-dir, so no M144 consent prompt and the
user's daily browser is untouched — and validates every measured done-when that a fake
cannot: lifecycle-wait overshoot (<10 ms), snapshot latency on ~450 elements, screenshot
CSS-pixel invariant on a real display, the dialog dance against a real blocking confirm(),
and the cassette bytes/call figure on real traffic. Not collected by pytest.
"""
from __future__ import annotations

import contextlib
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from harness.connect.cdp import Connection, WebSocketTransport
from harness.connect.endpoint import discover
from harness.connect.session import SessionRegistry
from harness.core.cassette import Recorder
from harness.core.outcome import HarnessError, NavigationFailed, NotSerializable
from harness.ops.page import Tab

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

FORM_PAGE = ("<!doctype html><title>bh live</title><body>"
             + "".join(f"<button id=b{i}>Button {i}</button>" for i in range(220))
             + "".join(f'<input id=i{i} placeholder="Field {i}">' for i in range(220))
             + "<select id=s1><option>one<option>two</select>"
             + "<a id=lnk href='/form' target=_blank>open new tab</a>"
             + "<button id=mut onclick=\"for(let i=0;i<7;i++)document.body.append("
               "document.createElement('p'))\">mutate</button>"
             + "<button id=dlg onclick=\"confirm('really?')\">dialog</button>"
             + "</body>")


class _Site(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/form":
            body = FORM_PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()          # empty body -> Chrome renders its own error page

    def log_message(self, *a):
        pass


results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, note: str = "") -> None:
    results.append((name, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<52} {note}")


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="bh-live-"))
    site = HTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{site.server_port}"

    chrome = subprocess.Popen(
        [CHROME, f"--user-data-dir={scratch}", "--remote-debugging-port=0",
         "--no-first-run", "--no-default-browser-check", "--window-size=1200,800",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while not (scratch / "DevToolsActivePort").exists():
            if time.monotonic() > deadline:
                print("Chrome never wrote DevToolsActivePort")
                return 1
            time.sleep(0.1)
        time.sleep(0.3)

        # our own discovery, against the scratch profile (endpoint.py live)
        r = discover({"BH_PROFILE_DIRS": str(scratch)})
        check("discovery via DevToolsActivePort", r.strategy == "profile", r.ws_url)

        tape = scratch / "cassette.jsonl"
        conn = Connection(Recorder(WebSocketTransport(r.ws_url), tape)).start()
        registry = SessionRegistry(conn)
        registry.discover()

        targets = conn.request("Target.getTargets")["targetInfos"]
        tid = next(t["targetId"] for t in targets if t["type"] == "page")
        session = registry.ready_session(tid)          # Phase 1 against real Chrome
        check("ready_session on real Chrome", session.live,
              f"domains={','.join(session.domains)}")

        tab = Tab(conn, registry, tid)

        # --- item 16: goto -------------------------------------------------
        nav = tab.goto(f"{base}/form")
        check("goto returns requested+landed", nav["landed"] == f"{base}/form", str(nav))

        # --- item 15: top-level await (same-origin now, about:blank cannot fetch)
        got = tab.js(f"const r = await fetch('{base}/form'); r.status")
        check("js: top-level await fetch (replMode)", got == 200, f"status={got}")

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        free_port = s.getsockname()[1]
        s.close()                       # ephemeral, not on Chrome's restricted-port list
        try:
            tab.goto(f"http://127.0.0.1:{free_port}/nope", timeout=10.0)
            check("goto refused port raises typed", False, "no exception")
        except NavigationFailed as e:
            check("goto refused port raises typed", "ERR_CONNECTION_REFUSED" in str(e),
                  str(e)[:60])

        try:
            tab.goto(f"{base}/missing", timeout=10.0)
            check("goto empty-404 raises typed", False, "no exception")
        except NavigationFailed as e:
            check("goto empty-404 raises typed", True,
                  f"landed={e.observed.get('landed', '')[:40]} {str(e)[:40]}")

        # --- item 19: wait overshoot ---------------------------------------
        with tab._armed(lambda m: m.get("method") == "Page.lifecycleEvent") as w:
            tab.cdp("Page.navigate", {"url": f"{base}/form"})
            hit = w.wait_match(lambda m: (m.get("params") or {}).get("name") == "load", 15.0)
            t_return = time.perf_counter()
        t_event = next(t for t, m in w.hits if m is hit)
        overshoot_ms = (t_return - t_event) * 1000
        check("event-driven wait overshoot < 10ms", overshoot_ms < 10,
              f"{overshoot_ms:.2f}ms (v1 polled at 300ms)")

        # --- item 20: snapshot ---------------------------------------------
        from harness.ops.page import SNAPSHOT_JS
        t0 = time.perf_counter()
        els = tab.snapshot()
        rt_ms = (time.perf_counter() - t0) * 1000
        timed = tab.js("(() => {const t0 = performance.now();"
                       f"const r = {SNAPSHOT_JS};"
                       "return {ms: performance.now() - t0, n: r.length};})()")
        check("snapshot ~450 elements", len(els) in range(440, 460), f"{len(els)} elements")
        check("snapshot in-page < 10ms", timed["ms"] < 10,
              f"{timed['ms']:.1f}ms in-page, {rt_ms:.1f}ms round trip")

        # --- item 17: deltas ------------------------------------------------
        mut = next(e for e in els if e["name"] == "mutate")
        delta = tab.click_ref(mut["ref"])
        check("click delta reports DOM mutations", (delta["dom_mutations"] or 0) >= 7,
              f"dom_mutations={delta['dom_mutations']}")

        dlg = next(e for e in els if e["name"] == "dialog")
        delta = tab.click_ref(dlg["ref"])
        check("dialog dance vs real blocking confirm()",
              delta["dialog"] == {"type": "confirm", "message": "really?"},
              str(delta["dialog"]))
        alive = tab.js("1+1")
        check("page responsive after dialog", alive == 2, "")

        lnk = next(e for e in els if e["tag"] == "a")
        delta = tab.click_ref(lnk["ref"], settle=1.0)
        check("click delta reports new tab", len(delta["new_targets"]) == 1,
              f"new_targets={delta['new_targets']}")

        # --- item 18: refs survive navigation --------------------------------
        tab.goto(f"{base}/form")
        els2 = tab.snapshot()
        fresh = tab.click_ref(els2[0]["ref"])
        check("refs usable after navigation (reinstalled runtime)",
              fresh["url_after"] is not None, "")

        # --- item 21: screenshot ---------------------------------------------
        dpr = tab.js("devicePixelRatio")
        css_w = tab.js("innerWidth")
        tab.capture_screenshot(scratch / "cold.jpeg")   # first shot pays raster warm-up
        t0 = time.perf_counter()
        shot = tab.capture_screenshot(scratch / "shot.jpeg")
        shot_ms = (time.perf_counter() - t0) * 1000
        sips = subprocess.run(["sips", "-g", "pixelWidth", str(scratch / "shot.jpeg")],
                              capture_output=True, text=True, check=False)
        px_w = int(sips.stdout.rsplit(None, 1)[-1])
        check("screenshot px == CSS px (any display)", px_w == css_w,
              f"dpr={dpr} css={css_w} out={px_w} {shot['bytes']}B {shot_ms:.0f}ms")

        # --- informational: DOM node via returnByValue -----------------------
        try:
            node = tab.js("document.body")
            check("js(document.body) does not silently None", node is not None,
                  f"chrome serialised it to {json.dumps(node)[:40]}")
        except (NotSerializable, HarnessError) as e:
            check("js(document.body) does not silently None", True,
                  f"typed: {type(e).__name__}")

        # --- item 27 caveat: real bytes/call ---------------------------------
        conn.close()
        frames = tape.read_text().splitlines()
        sends = sum(1 for line in frames if '"t": "send"' in line)
        size = tape.stat().st_size
        check("cassette on real traffic", sends > 40,
              f"{size}B / {sends} calls = {size // max(sends, 1)}B/call")

    finally:
        chrome.terminate()
        with contextlib.suppress(Exception):
            chrome.wait(5)
        site.shutdown()
        shutil.rmtree(scratch, ignore_errors=True)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed"
          + (f" — FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
