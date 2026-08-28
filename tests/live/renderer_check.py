"""`ensure_renderer()` against real Chrome.

Chrome's Memory Saver discards inactive background tabs; the target survives and the
renderer does not, so every renderer-served command hangs for its whole timeout. Over 100
postings that was `Page.navigate did not answer in 25.0s`, 11 of 18 workflow failures, on
a run that opens background tabs by design and leaves them idle between items.

**A true discard cannot be forced on demand** — CDP has no `Target.discardTarget`, which
is the same reason the probe exists at all: there is no `discarded` flag to read. What can
be reproduced faithfully is the condition the probe actually tests, a renderer that does
not answer inside the probe window, which a blocked main thread produces exactly.

    BH_HEADLESS=1 uv run python tests/live/renderer_check.py
"""
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests" / "live")]

PAGE = b"""<!doctype html><meta charset=utf-8><title>Idle</title><h1>idle tab</h1>
<script>
  // Block the main thread on demand: the renderer stops answering CDP exactly as a
  // discarded one does, without needing Chrome to actually discard anything.
  window.block = ms => { const until = Date.now() + ms; while (Date.now() < until); };
</script>"""


class _Site(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *a):
        pass


site = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
threading.Thread(target=site.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{site.server_port}/"
scratch = Path(tempfile.mkdtemp(prefix="bh-renderer-"))
os.environ.setdefault("BH_HEADLESS", "1")
os.environ["BH_PROFILE_DIRS"] = str(scratch)

import _browser

_browser.launch(scratch, window="1000,800")
ok = fail = 0


def check(label, condition, got=""):
    global ok, fail
    if condition:
        ok += 1
        print(f"  PASS  {label:<56} {got}")
    else:
        fail += 1
        print(f"  FAIL  {label:<56} {got}")


try:
    from harness.core.outcome import HarnessError
    from harness.session import Session

    session = Session("renderer")
    tab = session.tab()
    tab.goto(base)

    check("a live renderer answers the probe", tab.ensure_renderer() == "responsive")

    # The property that lets the probe guess wrong cheaply: reactivation must be harmless
    # on a tab that never needed it. (A sub-millisecond probe does not reliably force the
    # miss — a warm local renderer answers inside it — so the false-positive *branch* is
    # covered deterministically in tests/unit/test_query.py instead.)
    session.conn.request("Target.activateTarget", {"targetId": tab.target_id})
    check("reactivating a live tab leaves it working",
          tab.ensure_renderer() == "responsive")
    check("and it still drives afterwards",
          tab.goto(base)["landed"].startswith("http://127.0.0.1"))

    # A renderer that will not answer inside the probe window: a blocked main thread
    # produces exactly the condition the probe tests for, without needing a real discard.
    def stall(t, ms):
        def run():
            try:
                t.js(f"window.block({ms})", timeout=ms / 1000 + 6)
            except HarnessError:
                pass                       # the stall is the point; its outcome is not
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        time.sleep(0.4)                    # let the block start before probing
        return thread

    busy = Session("renderer-busy").tab()
    busy.goto(base)
    stalled = stall(busy, 2500)
    check("a stalled renderer is not reported responsive",
          busy.ensure_renderer(probe=0.5, revive=15.0) in ("revived", "unrecoverable"))
    stalled.join(timeout=15)
    check("and the tab drives again once the stall clears",
          busy.goto(base)["landed"].startswith("http://127.0.0.1"))

finally:
    _browser.kill(scratch)
    site.shutdown()

print(f"\n{ok}/{ok + fail} passed")
sys.exit(1 if fail else 0)
