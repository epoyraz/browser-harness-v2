"""Every SKILL.md example must actually run. A doc that drifts is worse than none."""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _browser

site = ThreadingHTTPServer(("127.0.0.1",0), partial(SimpleHTTPRequestHandler, directory=str(ROOT/"tests"/"fixtures")))
threading.Thread(target=site.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{site.server_port}"
#: See check.py — Windows occlusion throttling drops Input.dispatchMouseEvent.
scratch = Path(tempfile.mkdtemp(prefix="bh-skill-"))
# /tmp keeps the AF_UNIX sun_path inside its 104-byte budget; Windows has no such limit.
runtime = Path(tempfile.mkdtemp(prefix="bhs-", dir=None if os.name == "nt" else "/tmp"))
_browser.launch(scratch)
env={**os.environ,"PYTHONPATH":str(ROOT),"BH_RUNTIME_DIR":str(runtime),
     "BH_PROFILE_DIRS":str(scratch),"BU_CDP_URL":"","BU_CDP_WS":"","BU_NAME":"skillcheck"}
def bh(s): return subprocess.run([sys.executable,"-m","harness.cli.main","-"],input=s,
    capture_output=True,text=True,check=False,cwd=str(ROOT),env=env,timeout=120)
try:
    while not (scratch/"DevToolsActivePort").exists(): time.sleep(0.1)
    time.sleep(0.4)
    cases = {
      "quickstart": f'goto("{base}/personio.html")\nprint(page_text()[:60])',
      "typed error": ('from harness.core.outcome import Class, HarnessError\n'
                      'try:\n    goto("http://127.0.0.1:9/x")\n'
                      'except HarnessError as e:\n    print(e.cls is Class.NAVIGATION_FAILED, "landed" in e.observed)'),
      "read the page": f'goto("{base}/abacus.html")\nprint(len(snapshot()), len(page_text())>0, form_schema()["verdict"]["is_form"])',
      "see (two channels)": f'goto("{base}/widgets.html")\no = see("/tmp/bh_skill_see.jpg")\nprint(len(o["elements"]), o["marked"], o["bytes"] > 0, js("!document.getElementById(\'__bh_marks\')"))',
      "act": f'goto("{base}/personio.html")\nr=snapshot()[0]\nprint(type(click_ref(r["ref"]))is dict); press_key("Tab"); print(scroll(200)["y"]>=0)',
      "three tiers": f'goto("{base}/abacus.html")\ns=form_schema()\nf=[x for x in s["fields"] if x["kind"]=="text"][0]\nprint([set_value(f["ref"],"x",mode=m).ok for m in ("value","insert","type")])',
      "fill a form": f'''goto("{base}/abacus.html")
s = require_form(form_schema())
by = {{f["label"]: f for f in s["fields"]}}
out = fill_form([{{"ref": by["Vorname *"]["ref"], "value": "Enes"}},
                 {{"ref": by["Anrede *"]["ref"], "label": "Herr"}}])
print(out.ok, out.observed["succeeded"])''',
      "combobox": f'goto("{base}/combobox.html")\ns = form_schema()\nc = [f for f in s["fields"] if f["kind"]=="combobox"][0]\no = select_option(c["ref"], "Referral from a friend")\nprint(o.ok, js("document.getElementById(\'c1\').textContent"))',
      "tabs": f'''t = new_tab("{base}/personio.html")
use_tab(t.target_id); print(len(targets())>=1, t.target_id[:4]!=""); close_tab()''',
      "fetch_all": f'goto("{base}/abacus.html")\no=fetch_all(["{base}/personio.html","{base}/abacus.html"])\nprint(o.ok, o.observed["succeeded"])',
      "not_a_form": f'goto("{base}/notaform.html")\nfrom harness.core.outcome import NotAForm\ntry:\n    require_form(form_schema())\n    print("NO RAISE")\nexcept NotAForm as e:\n    print("raised", e.cls.value)',
      "top-level await": f'goto("{base}/personio.html")\nprint(js("await (async () => 41 + 1)()"))',
    }
    bad=0
    for label, src in cases.items():
        r = bh(src)
        ok = r.returncode==0
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<18} {(r.stdout.strip()[:64] if ok else r.stderr.strip()[-150:])}")
        bad += 0 if ok else 1
    # journal + trace, as documented
    jr = runtime/"run.jsonl"; env["BH_JOURNAL"]=str(jr)
    bh(f'goto("{base}/abacus.html")\nform_schema()')
    tr = subprocess.run([sys.executable,"-m","harness.cli.main","trace",str(jr)],
                        capture_output=True,text=True,check=False,cwd=str(ROOT),env=env)
    ok = tr.returncode==0 and "cdp=" in tr.stdout
    print(f"  {'PASS' if ok else 'FAIL'}  {'BH_JOURNAL + bh trace':<18} {tr.stdout.strip().splitlines()[0][:64] if ok else tr.stderr[-120:]}")
    bad += 0 if ok else 1
    print(f"\n{len(cases)+1-bad}/{len(cases)+1} SKILL.md examples run")
    sys.exit(1 if bad else 0)
finally:
    _browser.kill(scratch); site.shutdown(); shutil.rmtree(scratch,ignore_errors=True); shutil.rmtree(runtime,ignore_errors=True)
