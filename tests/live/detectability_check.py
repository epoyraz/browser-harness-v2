"""Isolate WHAT sets navigator.webdriver: the flag, or the CDP attach? The page reports
back over plain HTTP, so no CDP is involved in the measurement itself."""
import queue
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _browser

results = queue.Queue()
PROBE = """<!doctype html><meta charset=utf-8><body><script>
const send = (tag) => fetch('/report?' + new URLSearchParams({
  tag, webdriver: String(navigator.webdriver),
  globals: Object.getOwnPropertyNames(window).filter(k=>/^__bh/.test(k)).join(',')}));
send('onload');
setTimeout(() => send('after3s'), 3000);
</script></body>"""

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/report"):
            from urllib.parse import parse_qs, urlparse
            results.put({k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()})
            self.send_response(204); self.end_headers(); return
        b = PROBE.encode(); self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self,*a): pass

site = HTTPServer(("127.0.0.1",0), H); threading.Thread(target=site.serve_forever,daemon=True).start()
URL = f"http://127.0.0.1:{site.server_port}/"

def launch(extra):
    """Its own launcher, because this suite is the one case `_browser.launch` cannot
    serve: the whole point is to compare a Chrome *without* --remote-debugging-port
    against one with it, and without that flag there is no DevToolsActivePort to wait on.

    Still detached via `open`, for the same reason everything else here is: launching
    Chrome as a child makes macOS attribute its file access to the terminal, and the
    unanswered TCC prompt that follows revokes the terminal's Desktop permission.
    """
    scratch = Path(tempfile.mkdtemp(prefix="bh-det3-"))
    _browser.reserve(scratch)
    args = [f"--user-data-dir={scratch}", "--no-first-run",
            f"--download-directory={scratch}", "--no-default-browser-check", *extra, URL]
    try:
        subprocess.run(["/usr/bin/open", "-na", _browser.CHROME, "--args", *args],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        _browser.kill(scratch)
        raise
    return scratch

for label, extra in [("no remote debugging at all", []),
                     ("--remote-debugging-port only (nobody attached)",
                      ["--remote-debugging-port=0"])]:
    while not results.empty(): results.get()
    scratch = launch(extra)
    try:
        r = results.get(timeout=25)
        print(f"{label:<48} webdriver={r['webdriver']:<6} globals={r.get('globals','')!r}")
    except queue.Empty:
        print(f"{label:<48} NO REPORT")
    finally:
        _browser.kill(scratch)

# now: attach with v2 and re-probe the SAME page after attach
from harness.connect.cdp import Connection, WebSocketTransport
from harness.connect.endpoint import discover
from harness.connect.session import SessionRegistry

while not results.empty(): results.get()
scratch = launch(["--remote-debugging-port=0"])
try:
    first = results.get(timeout=25)
    while not (scratch/"DevToolsActivePort").exists(): time.sleep(0.1)
    r = discover({"BH_PROFILE_DIRS": str(scratch)})
    conn = Connection(WebSocketTransport(r.ws_url)).start()
    reg = SessionRegistry(conn)
    tid = next(t["targetId"] for t in conn.request("Target.getTargets")["targetInfos"] if t["type"]=="page")
    reg.ready_session(tid)                       # attach + enable domains
    after = results.get(timeout=25)              # the page's own 3-second re-probe
    print(f"{'v2 attached (page reports itself, 3s later)':<48} webdriver={after['webdriver']:<6} globals={after.get('globals','')!r}")
    conn.close()
finally:
    _browser.kill(scratch); site.shutdown()
