"""Recording, video, telemetry, extensions and the two reliability helpers — live.

Everything here is the v1-parity work: a real `bh` run against real Chrome that records
itself, renders an mp4 with ffmpeg, aggregates its own journal, loads an agent-written
helper, waits on an event instead of a sleep, and reaches into a cross-origin iframe.

Run manually: `.venv/bin/python tests/live/record_check.py`
"""
from __future__ import annotations

import json
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
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _browser

FIXTURES = ROOT / "tests" / "fixtures"
results: list[tuple[str, bool, str]] = []

#: A page that renders late and hosts a cross-origin frame — the two conditions a fixed
#: sleep handles badly and a DOM query cannot see into at all.
SLOW_PAGE = """<!doctype html><meta charset=utf-8><title>Late</title><body>
<h1>loading…</h1>
<iframe src="__IFRAME__" style="width:300px;height:120px"></iframe>
<script>
setTimeout(() => {
  const d = document.createElement('div');
  d.id = 'late'; d.textContent = 'ready';
  d.style.cssText = 'padding:20px;background:#efe';
  document.body.appendChild(d);
  document.querySelector('h1').textContent = 'ready';
}, 1800);
</script></body>"""

CLOSED_SHADOW_PAGE = """<!doctype html><meta charset=utf-8><title>Closed shadow</title>
<body><div id="host"></div><script>
const root = document.querySelector('#host').attachShadow({mode: 'closed'});
const frame = document.createElement('iframe');
frame.src = '__IFRAME__';
frame.style.cssText = 'width:300px;height:120px';
root.append(frame);
</script></body>"""

DYNAMIC_FRAME_PAGE = """<!doctype html><meta charset=utf-8><title>Dynamic frame</title>
<body><p>frame will be inserted by the live check</p></body>"""


class _QuietServer(ThreadingHTTPServer):
    """Hide only the connection resets Chrome causes while scratch tabs close."""

    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exception(),
                      (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def check(name: str, ok: bool, note: str = "") -> None:
    results.append((name, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<54} {note}")


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="bh-rec-"))
    runtime = Path(tempfile.mkdtemp(prefix="bhr-"))
    recs = Path(tempfile.mkdtemp(prefix="bhrecs-"))
    origin_a = _QuietServer(("127.0.0.1", 0),
                            partial(SimpleHTTPRequestHandler, directory=str(FIXTURES)))
    threading.Thread(target=origin_a.serve_forever, daemon=True).start()
    other = f"http://localhost:{origin_a.server_port}/personio.html"

    slow_dir = Path(tempfile.mkdtemp(prefix="bhslow-"))
    (slow_dir / "slow.html").write_text(
        SLOW_PAGE.replace("__IFRAME__", other), encoding="utf-8")
    (slow_dir / "closed-shadow.html").write_text(
        CLOSED_SHADOW_PAGE.replace("__IFRAME__", other), encoding="utf-8")
    (slow_dir / "dynamic-frame.html").write_text(DYNAMIC_FRAME_PAGE, encoding="utf-8")
    # localhost vs 127.0.0.1 is a different HOST, which is what site isolation keys on.
    # Two ports on the same host are one site and stay in-process — the first version of
    # this fixture made that mistake and could never produce an OOPIF to find.
    origin_b = _QuietServer(("127.0.0.1", 0),
                            partial(SimpleHTTPRequestHandler, directory=str(slow_dir)))
    threading.Thread(target=origin_b.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{origin_b.server_port}"

    helpers = Path(tempfile.mkdtemp(prefix="bhhelp-")) / "helpers.py"
    helpers.write_text(
        "def page_summary():\n"
        '    """Written the way an agent would: calls the surface, no imports."""\n'
        '    return {"title": js("document.title"), "elements": len(snapshot())}\n',
        encoding="utf-8")

    # Two loopback ports are the SAME site, so an iframe between them stays in-process.
    # Force site isolation to get a real OOPIF to test against.
    _browser.launch(scratch, window="1100,760", extra=["--site-per-process"])
    env = {**os.environ, "PYTHONPATH": str(ROOT), "BH_RUNTIME_DIR": str(runtime),
           "BH_PROFILE_DIRS": str(scratch), "BU_CDP_URL": "", "BU_CDP_WS": "",
           "BH_RECORDINGS": str(recs), "BH_HELPERS": str(helpers),
           "BU_NAME": "reccheck"}

    def bh(script: str, extra_env: dict | None = None, timeout: float = 180):
        return subprocess.run([sys.executable, "-m", "harness.cli.main", "-"],
                              input=script, capture_output=True, text=True, check=False,
                              cwd=str(ROOT), env={**env, **(extra_env or {})},
                              timeout=timeout)

    def cli(*a, timeout: float = 120):
        return subprocess.run([sys.executable, "-m", "harness.cli.main", *a],
                              capture_output=True, text=True, check=False,
                              cwd=str(ROOT), env=env, timeout=timeout)

    try:
        deadline = time.monotonic() + 20
        while not (scratch / "DevToolsActivePort").exists():
            if time.monotonic() > deadline:
                print("Chrome never wrote DevToolsActivePort")
                return 1
            time.sleep(0.1)
        time.sleep(0.4)

        # ---- 1. wait_for beats the sleep it replaces -----------------------
        r = bh(f"""
import json, time
goto("{base}/slow.html")
t0 = time.perf_counter()
w = wait_for("#late", state="visible", timeout=15)
print(json.dumps({{"ms": round((time.perf_counter()-t0)*1000), "imm": w["immediate"],
                   "text": js("document.getElementById('late').textContent")}}))
""")
        ok = r.returncode == 0 and '"text": "ready"' in r.stdout
        got = json.loads(r.stdout.strip().splitlines()[-1]) if ok else {}
        check("wait_for wakes on the mutation, not on a guess", ok and not got.get("imm"),
              (f"{got.get('ms')}ms for an element that appears at 1800ms"
               if ok else r.stderr.strip()[-140:]))

        r = bh(f'goto("{base}/slow.html")\nprint(wait_for("h1", timeout=5)["immediate"])')
        check("an already-satisfied wait returns without waiting",
              r.returncode == 0 and r.stdout.strip() == "True", r.stdout.strip()[:40])

        r = bh(f'goto("{base}/slow.html")\nwait_for("#nope", timeout=1.0)')
        check("a wait that never lands is a typed timeout",
              r.returncode == 1 and '"class": "timeout"' in r.stderr,
              r.stderr.replace("\n", " ")[:80])

        # ---- 2. the binding must not leak into the page -------------------
        r = bh(f"""
goto("{base}/slow.html")
wait_for("h1", timeout=5)
print(js("typeof window.__bhNotify"), js("typeof window.__bh"))
""")
        check("the wait binding is invisible to the page",
              r.returncode == 0 and r.stdout.strip() == "undefined undefined",
              r.stdout.strip()[:40])

        # ---- 3. cross-origin iframe reachable ------------------------------
        r = bh(f"""
import json
goto("{base}/slow.html")
wait_for("iframe", timeout=10)
fs = frames()
print(json.dumps({{"n": len(fs), "urls": [f["url"][:40] for f in fs]}}))
""")
        ok = r.returncode == 0 and '"n": 1' in r.stdout
        check("frames() surfaces the cross-origin iframe as a target", ok,
              (r.stdout.strip()[-60:] if ok else r.stderr.strip()[-140:]))

        r = bh(f"""
goto("{base}/slow.html")
wait_for("iframe", timeout=10)
print(len(js("document.body.innerText")) , end=" | ")
t = session.tab(frames()[0]["target_id"])
print(len(t.page_text()) > 20, t.js("document.title"))
""")
        check("the iframe's own content is readable once attached",
              r.returncode == 0 and "True" in r.stdout,
              (r.stdout.strip()[:60] if r.returncode == 0
               else r.stderr.strip().replace("\n", " ")[-150:]))

        r = bh(f"""
import json
goto("{base}/closed-shadow.html")
fs = frames()
print(json.dumps({{"n": len(fs), "urls": [f["url"] for f in fs]}}))
""")
        ok = r.returncode == 0 and '"n": 1' in r.stdout and "personio.html" in r.stdout
        check("frames() pierces a closed-shadow OOPIF host", ok,
              (r.stdout.strip()[-100:] if ok else r.stderr.strip().replace("\n", " ")[-160:]))

        r = bh(f"""
import json, threading, time
goto("{base}/dynamic-frame.html")
tab = session.tab()
timer = threading.Timer(0.08, lambda: tab.js('''(() => {{
  const frame = document.createElement('iframe');
  frame.src = {json.dumps(other)};
  document.body.append(frame);
  return true;
}})()'''))
timer.start()
t0 = time.perf_counter()
fs = tab.frames()
timer.join()
print(json.dumps({{"n": len(fs), "ms": round((time.perf_counter()-t0)*1000),
                   "urls": [f["url"] for f in fs]}}))
""")
        # Chrome announces the new OOPIF before its first navigation commits, so the
        # targetInfo URL may honestly still be empty. The target id is the discovery
        # contract; callers attach to it and observe navigation through that session.
        ok = r.returncode == 0 and '"n": 1' in r.stdout
        check("frames() catches an OOPIF inserted after its zero probe", ok,
              (r.stdout.strip()[-120:] if r.returncode == 0
               else r.stderr.strip().replace("\n", " ")[-160:]))

        # ---- 4. agent-written helper is first-class ------------------------
        r = bh(f'goto("{base}/slow.html")\nimport json; print(json.dumps(page_summary()))')
        ok = r.returncode == 0 and '"title"' in r.stdout
        check("an agent-written helper is in scope with no import", ok,
              (r.stdout.strip()[-60:] if ok else r.stderr.strip()[-140:]))

        r = bh("print(1)", {"BH_HELPERS": str(helpers.parent / "broken.py")})
        (helpers.parent / "broken.py").write_text("def x(:\n", encoding="utf-8")
        r = bh("print('still works')", {"BH_HELPERS": str(helpers.parent / "broken.py")})
        check("a broken helper file warns but never costs you the browser",
              r.returncode == 0 and "still works" in r.stdout and "failed to load" in r.stderr,
              r.stderr.strip().splitlines()[-1][:70] if r.stderr else "")

        # ---- 5. recording ---------------------------------------------------
        r = bh(f"""
goto("{base}/slow.html")
wait_for("#late", timeout=15)
scroll(300)
press_key("Tab")
goto("{other}")
s = form_schema()
by = {{f["label"]: f for f in s["fields"]}}
fill_form([{{"ref": by["First name"]["ref"], "value": "Enes"}},
           {{"ref": by["Last name"]["ref"], "value": "Poyraz"}}])
print("done")
""", {"BH_RECORD": "1"})
        rec_dirs = sorted([p for p in recs.iterdir() if p.is_dir()],
                          key=lambda p: p.stat().st_mtime, reverse=True)
        rec = rec_dirs[0] if rec_dirs else None
        frames_n = len(list(rec.glob("*.jpg"))) if rec else 0
        check("BH_RECORD=1 captures a frame per action",
              r.returncode == 0 and frames_n >= 5, f"{frames_n} frames in {rec.name if rec else '-'}")

        entries = []
        if rec and (rec / "session.jsonl").is_file():
            entries = [json.loads(x) for x in
                       (rec / "session.jsonl").read_text().splitlines() if x.strip()]
        framed = [e for e in entries if e.get("frame")]
        check("frames live ON the journal entry, not a parallel file",
              bool(framed) and not (rec / "events.jsonl").exists(),
              f"{len(framed)} call entries carry a frame")
        check("recorded calls carry url + title context",
              any(e.get("url") and e.get("title") for e in framed),
              next((f"{e['fn']} -> {e['title'][:28]}" for e in framed if e.get("title")), ""))

        # ---- 6. video --------------------------------------------------------
        out = cli("video", str(rec))
        mp4 = rec / "video.mp4"
        check("bh video renders a playable mp4",
              out.returncode == 0 and mp4.is_file() and mp4.stat().st_size > 5000,
              out.stdout.strip()[:88] if out.returncode == 0 else out.stderr.strip()[-140:])
        if mp4.is_file() and shutil.which("ffprobe"):
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries",
                 "format=duration:stream=codec_name,width,height,pix_fmt",
                 "-of", "json", str(mp4)], capture_output=True, text=True, check=False)
            info = json.loads(probe.stdout or "{}")
            st = (info.get("streams") or [{}])[0]
            dur = float((info.get("format") or {}).get("duration", 0))
            check("the mp4 is h264/yuv420p with even dimensions (i.e. it plays)",
                  st.get("codec_name") == "h264" and st.get("pix_fmt") == "yuv420p"
                  and st.get("width", 1) % 2 == 0 and st.get("height", 1) % 2 == 0,
                  f"{st.get('codec_name')} {st.get('width')}x{st.get('height')} "
                  f"{st.get('pix_fmt')} {dur:.1f}s")
            plan = json.loads((rec / "plan.json").read_text())
            check("hold times come from the journal's real gaps",
                  any(s["real"] > 1.0 for s in plan["shots"]),
                  f"real {plan['real_duration']:.1f}s -> shown {plan['duration']:.1f}s")

        # ---- 7. telemetry ---------------------------------------------------
        out = cli("stats", str(rec / "session.jsonl"))
        check("bh stats aggregates the journal it already had",
              out.returncode == 0 and "helper" in out.stdout and "calls" in out.stdout,
              out.stdout.strip().splitlines()[0][:70] if out.stdout else out.stderr[-90:])
        out = cli("stats", str(rec / "session.jsonl"), "--json")
        roll = json.loads(out.stdout or "{}")
        leaked = json.dumps(roll)
        check("telemetry carries no urls, args or JS source",
              "http://" not in leaked and "expression" not in leaked
              and "args" not in leaked,
              f"{roll.get('calls', 0)} calls, {len(roll.get('helpers', []))} helpers")

    finally:
        _browser.kill(scratch)
        origin_a.shutdown(); origin_b.shutdown()
        for d in (scratch, runtime, recs, slow_dir, helpers.parent):
            shutil.rmtree(d, ignore_errors=True)

    failed = [n for n, ok_, _ in results if not ok_]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed"
          + (f" — FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
