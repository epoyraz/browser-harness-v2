"""The gap this closes: until now the daemon had NEVER connected to a real browser.

Every other live suite drives `Connection` directly, and `Daemon` was exercised only
against a fake I wrote myself — which means D5 and D7 (one websocket, N clients, no
consent storm) were architecture, not fact. This runs the real thing: `bh` spawning a real
daemon against real Chrome, two independent client *processes* driving two tabs at once,
and the failure modes that only appear when a daemon outlives its clients.

Run manually: `.venv/bin/python tests/live/daemon_check.py`
"""
from __future__ import annotations

import contextlib
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

from harness.core import ipc

#: See check.py — Windows occlusion throttling drops Input.dispatchMouseEvent.
FIXTURES = ROOT / "tests" / "fixtures"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, note: str = "") -> None:
    results.append((name, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<56} {note}")


def bh(script: str, env: dict[str, str], timeout: float = 120) -> subprocess.CompletedProcess:
    """Run a script exactly the way an agent would: `bh <<'PY' … PY`."""
    return subprocess.run(
        [sys.executable, "-m", "harness.cli.main", "-"],
        input=script, capture_output=True, text=True, check=False,
        cwd=str(ROOT), env=env, timeout=timeout)


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="bh-daemon-"))
    runtime = Path(tempfile.mkdtemp(prefix="bhrt-", dir="/tmp"))   # short: AF_UNIX 104 bytes
    site = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler,
                                                         directory=str(FIXTURES)))
    threading.Thread(target=site.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{site.server_port}"

    _browser.launch(scratch, window="1200,900")
    env = {**os.environ, "PYTHONPATH": str(ROOT),
           "BH_RUNTIME_DIR": str(runtime),
           "BH_PROFILE_DIRS": str(scratch), "BU_CDP_URL": "", "BU_CDP_WS": ""}
    name = "livecheck"
    os.environ["BH_RUNTIME_DIR"] = str(runtime)
    try:
        deadline = time.monotonic() + 20
        while not (scratch / "DevToolsActivePort").exists():
            if time.monotonic() > deadline:
                print("Chrome never wrote DevToolsActivePort")
                return 1
            time.sleep(0.1)
        time.sleep(0.4)
        env["BU_NAME"] = name

        # ---- 1. a script auto-spawns the daemon and drives the browser ------
        t0 = time.perf_counter()
        r = bh(f"""
goto("{base}/personio.html")
print(json.dumps({{"title": js("document.title"),
                   "fields": len(form_schema()["fields"]),
                   "text": page_text()[:40]}}))
""".replace("json.dumps", "__import__('json').dumps"), env)
        spawn_ms = (time.perf_counter() - t0) * 1000
        ok = r.returncode == 0 and '"fields"' in r.stdout
        check("bh script auto-spawns the daemon and drives Chrome", ok,
              (r.stdout.strip()[:70] if ok else r.stderr.strip()[-160:]) + f"  {spawn_ms:.0f}ms")
        if not ok:
            return 1

        # ---- 2. the daemon outlives the client -----------------------------
        pong = ipc.ping(name)
        check("daemon survives its client and reports a live browser",
              bool(pong and pong.get("browser")), json.dumps(pong)[:80] if pong else "no pong")

        # ---- 3. second run reuses it (no second consent prompt, D7) --------
        t0 = time.perf_counter()
        r2 = bh('print(js("1+1"))', env)
        reuse_ms = (time.perf_counter() - t0) * 1000
        check("a second script reuses the daemon rather than spawning one",
              r2.returncode == 0 and r2.stdout.strip() == "2" and reuse_ms < spawn_ms,
              f"{reuse_ms:.0f}ms vs {spawn_ms:.0f}ms cold")

        # ---- 4. TWO CLIENT PROCESSES, TWO TABS, ONE CONNECTION -------------
        # TODO 7's done-when, finally against real Chrome instead of a fake.
        prep = bh(f"""
a = new_tab("{base}/personio.html")
b = new_tab("{base}/abacus.html")
print(__import__('json').dumps([a.target_id, b.target_id]))
""", env)
        tids = json.loads(prep.stdout.strip().splitlines()[-1])
        script = """
import json, time
t = use_tab(TID)
t0 = time.perf_counter()
vals = [t.js("document.title") for _ in range(12)]
print(json.dumps({"tid": t.target_id, "title": vals[0],
                  "ms": round((time.perf_counter()-t0)*1000, 1)}))
"""
        out: dict[str, dict] = {}
        lock = threading.Lock()

        def client(tid: str) -> None:
            res = bh(script.replace("TID", repr(tid)), env)
            with lock:
                out[tid] = (json.loads(res.stdout.strip().splitlines()[-1])
                            if res.returncode == 0 and res.stdout.strip() else
                            {"error": res.stderr[-200:]})

        t0 = time.perf_counter()
        threads = [threading.Thread(target=client, args=(t,)) for t in tids]
        for th in threads:
            th.start()
        for th in threads:
            th.join(120)
        wall = (time.perf_counter() - t0) * 1000

        for t in tids:
            if "error" in out.get(t, {}):
                print(f"    client {t[:8]} stderr: {out[t]['error'][-200:]}")
        titles = {t: out.get(t, {}).get("title") for t in tids}
        distinct = len(set(titles.values())) == 2 and all(titles.values())
        check("two client PROCESSES drive two tabs over one connection", distinct,
              " | ".join(f"{str(v)[:26]}" for v in titles.values()))
        check("neither client was served the other's tab",
              all(out.get(t, {}).get("tid") == t for t in tids),
              f"{wall:.0f}ms wall for both")

        # ---- 5. events reach the client through the daemon -----------------
        r5 = bh(f"""
import json
goto("{base}/personio.html")
els = snapshot()
btn = [e for e in els if e["tag"] == "button"][0]
d = click_ref(btn["ref"], settle=0.4)
print(json.dumps({{"navigated": d["navigated"], "mutations": d["dom_mutations"],
                   "url_after": bool(d["url_after"])}}))
""", env)
        ok5 = r5.returncode == 0 and '"url_after": true' in r5.stdout
        check("event-driven click delta works over the daemon", ok5,
              (r5.stdout.strip()[-70:] if ok5 else r5.stderr.strip()[-160:]))

        # ---- 6. a typed failure crosses process + IPC intact ---------------
        r6 = bh('goto("http://127.0.0.1:9/nope")', env)
        ok6 = r6.returncode == 1 and '"class": "navigation_failed"' in r6.stderr
        check("a typed failure survives the IPC hop as a class, not a string", ok6,
              r6.stderr.strip().replace("\n", " ")[:90])

        # ---- 7. batching still batches through the daemon ------------------
        r7 = bh(f"""
import json
goto("{base}/abacus.html")
s = form_schema()
by = {{f["name"]: f for f in s["fields"]}}
plan = [{{"ref": by["customeraddressshoppervorname"]["ref"], "value": "Enes"}},
        {{"ref": by["customeraddressshoppernachname"]["ref"], "value": "Poyraz"}},
        {{"ref": by["customeraddressshopperemail"]["ref"], "value": "e@example.ch"}}]
o = fill_form(plan)
print(json.dumps({{"ok": o.ok, "n": o.observed["succeeded"]}}))
""", env)
        check("form_schema + fill_form work end-to-end via `bh`",
              r7.returncode == 0 and '"ok": true' in r7.stdout and '"n": 3' in r7.stdout,
              (r7.stdout.strip()[-50:] if r7.returncode == 0 else r7.stderr.strip()[-160:]))

        # ---- 8. upload_file, the surface gap the post made famous ----------
        sample = runtime / "cv.txt"
        sample.write_text("not a real CV")
        r8 = bh(f"""
import json
goto("{base}/personio.html")
s = form_schema()
r = cdp("Runtime.evaluate", {{"expression": "1"}})   # keep the world warm
els = snapshot()
inp = [e for e in els if e.get("type") == "file"][0]
print(json.dumps(upload_file(inp["ref"], {str(sample)!r})))
""", env)
        check("upload_file attaches a file without the OS picker",
              r8.returncode == 0 and "cv.txt" in r8.stdout,
              (r8.stdout.strip()[-60:] if r8.returncode == 0 else r8.stderr.strip()[-160:]))

    finally:
        with contextlib.suppress(Exception):
            bh("pass", env, timeout=10)
        for f in runtime.glob("*.sock"):
            with contextlib.suppress(Exception):
                f.unlink()
        _browser.kill(scratch)
        site.shutdown()
        shutil.rmtree(scratch, ignore_errors=True)
        shutil.rmtree(runtime, ignore_errors=True)

    failed = [n for n, ok_, _ in results if not ok_]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed"
          + (f" — FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
