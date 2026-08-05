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
    scratch = Path(tempfile.mkdtemp(prefix="bh-det3-"))
    args = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            f"--user-data-dir={scratch}", "--no-first-run",
            "--no-default-browser-check"] + extra + [URL]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL), scratch

for label, extra in [("no remote debugging at all", []),
                     ("--remote-debugging-port only (nobody attached)",
                      ["--remote-debugging-port=0"])]:
    while not results.empty(): results.get()
    ch, scratch = launch(extra)
    try:
        r = results.get(timeout=25)
        print(f"{label:<48} webdriver={r['webdriver']:<6} globals={r.get('globals','')!r}")
    except queue.Empty:
        print(f"{label:<48} NO REPORT")
    finally:
        ch.terminate(); time.sleep(0.5)

# now: attach with v2 and re-probe the SAME page after attach
from harness.connect.cdp import Connection, WebSocketTransport
from harness.connect.endpoint import discover
from harness.connect.session import SessionRegistry

while not results.empty(): results.get()
ch, scratch = launch(["--remote-debugging-port=0"])
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
    ch.terminate(); site.shutdown()
