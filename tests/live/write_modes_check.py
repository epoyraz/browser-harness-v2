"""Does v2's one-shot write differ observably from v1's keystrokes? Measure, don't assert."""
import json
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

#: See check.py — Windows occlusion throttling drops Input.dispatchMouseEvent.
from harness.connect.cdp import Connection, WebSocketTransport
from harness.connect.endpoint import discover
from harness.connect.session import SessionRegistry
from harness.ops.forms import fill_form, form_schema, set_value
from harness.ops.page import Tab

PAGE = """<!doctype html><meta charset=utf-8><body>
<label for=a>Plain</label><input id=a name=a>
<label for=b>Masked (formats as you type)</label><input id=b name=b>
<label for=c>Typeahead</label><input id=c name=c autocomplete=off>
<div id=drop></div>
<button type=submit>Go</button>
<script>
window.log = {trusted: [], keydowns: 0, inputs: 0, maskRuns: 0, typeaheadOpens: 0};
for (const id of ['a','b','c']) {
  const el = document.getElementById(id);
  el.addEventListener('keydown', () => log.keydowns++);
  el.addEventListener('input', e => { log.inputs++; log.trusted.push(e.isTrusted); });
}
// a phone-style mask that only works incrementally
document.getElementById('b').addEventListener('input', e => {
  log.maskRuns++;
  const d = e.target.value.replace(/\\D/g,'').slice(0,10);
  e.target.value = d.replace(/(\\d{3})(\\d{3})(\\d{0,4})/, (m,x,y,z)=> z? `${x}-${y}-${z}`:`${x}-${y}`);
});
// a typeahead that opens on each keystroke
document.getElementById('c').addEventListener('keyup', () => {
  log.typeaheadOpens++;
  document.getElementById('drop').textContent = 'suggestions for ' + c.value;
});
</script></body>"""

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        b = PAGE.encode(); self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self,*a): pass

site = HTTPServer(("127.0.0.1",0), H); threading.Thread(target=site.serve_forever,daemon=True).start()
scratch = Path(tempfile.mkdtemp(prefix="bh-trust-"))
_browser.launch(scratch)
try:
    while not (scratch/"DevToolsActivePort").exists(): time.sleep(0.1)
    time.sleep(0.4)
    r = discover({"BH_PROFILE_DIRS": str(scratch)})
    conn = Connection(WebSocketTransport(r.ws_url)).start(); reg = SessionRegistry(conn)
    tid = next(t["targetId"] for t in conn.request("Target.getTargets")["targetInfos"] if t["type"]=="page")
    tab = Tab(conn, reg, tid)
    base = f"http://127.0.0.1:{site.server_port}/"

    for mode in ("v2 one-shot", "v2 mode=insert", "v2 mode=type"):
        tab.goto(base); time.sleep(0.3)
        sc = form_schema(tab)
        by = {f["label"]: f for f in sc["fields"]}
        MODE = "insert" if "insert" in mode else "type"
        if mode == "v2 one-shot":
            fill_form(tab, [{"ref": by["Plain"]["ref"], "value": "hello"},
                            {"ref": by["Masked (formats as you type)"]["ref"], "value": "4155551234"},
                            {"ref": by["Typeahead"]["ref"], "value": "zur"}], recheck=0)
        else:
            for lbl, val in [("Plain","hello"),("Masked (formats as you type)","4155551234"),("Typeahead","zur")]:
                set_value(tab, by[lbl]["ref"], val, mode=MODE, recheck=0)
        time.sleep(0.3)
        log = tab.js("({...window.log, values: [a.value, b.value, c.value], drop: drop.textContent})")
        print(f"\n{mode}:")
        print("  ", json.dumps(log, ensure_ascii=False))
    conn.close()
finally:
    _browser.kill(scratch); site.shutdown()
