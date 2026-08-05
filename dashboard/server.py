"""Local dashboard for browser-harness application runs.

    python -m dashboard.server          # from the repo root
    python dashboard/server.py

Opens on http://127.0.0.1:8765. Red = not started, amber = in progress, green = filled,
each with a screenshot as proof. Nothing is ever submitted and no CV is ever uploaded:
attaching a file POSTs it to the ATS on selection, not on submit, which would create a
candidate record for an application that was never made.

Applicant details are read from `dashboard/applicant.json` (gitignored) or the
BH_APPLICANT_* environment variables, and are editable live in the UI. Nothing personal
is stored in this file.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SHOTS = HERE / "shots"
PORT = int(os.environ.get("BH_DASH_PORT", "8765"))

#: Three runs at ten parallel clients leaked tabs and killed the browser. This is a scar.
MAX_PARALLEL = int(os.environ.get("BH_DASH_PARALLEL", "4"))

WORK = Path(os.environ.get("BH_DASH_WORK") or (Path(tempfile.gettempdir()) / "bh-dashboard"))
PROFILE = WORK / "profile"
RUNTIME = WORK / "runtime"


def find_chrome() -> str:
    if env := os.environ.get("BH_CHROME"):
        return env
    candidates = {
        "win32": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"],
        "darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
    }.get(sys.platform, ["/usr/bin/google-chrome", "/usr/bin/chromium"])
    return next((c for c in candidates if Path(c).exists()), "")


CHROME = find_chrome()


def load_applicant() -> dict:
    """Never hardcode a person into the repo: file first, then env, then blanks."""
    base = {"first": "", "last": "", "email": "", "phone": "", "city": "",
            "cover": "Interested in this role — sent via my application assistant."}
    cfg = HERE / "applicant.json"
    if cfg.exists():
        try:
            base.update(json.loads(cfg.read_text(encoding="utf-8")))
        except ValueError:
            pass
    for k in list(base):
        if v := os.environ.get(f"BH_APPLICANT_{k.upper()}"):
            base[k] = v
    return base


STATE: dict = {"query": "", "parsed": {}, "jobs": [], "phase": "idle", "message": "",
               "applicant": load_applicant()}
LOCK = threading.Lock()

CITIES = ["Zürich", "Zurich", "Bern", "Basel", "Luzern", "Lausanne", "Genève", "Genf",
          "Winterthur", "St. Gallen", "Zug", "Baden", "Aarau", "Chur", "Lugano"]


def parse_prompt(prompt: str) -> dict:
    """One free-text line -> joblens params. Echoed back in the UI so a bad parse is
    visible rather than silent."""
    text = prompt.strip()
    city = ""
    for c in CITIES:
        if re.search(rf"\b{re.escape(c)}\b", text, re.IGNORECASE):
            city = "Zürich" if c.lower() in ("zurich", "zürich") else c
            text = re.sub(rf"\b{re.escape(c)}\b", " ", text, flags=re.IGNORECASE)
            break
    since = ""
    if re.search(r"\b(24\s*h|today|heute|last 24)\b", text, re.IGNORECASE):
        since = "1"
    elif re.search(r"\b(week|woche|7\s*(days|tage)?)\b", text, re.IGNORECASE):
        since = "7"
    elif re.search(r"\b(month|monat|30\s*(days|tage)?)\b", text, re.IGNORECASE):
        since = "30"
    text = re.sub(r"\b(in|last|letzte[nr]?|past|week|woche|month|monat|days?|tage?|"
                  r"jobs?|stellen|find|search|suche|me|the|top)\b", " ", text, flags=re.IGNORECASE)
    return {"title": " ".join(text.split()) or "Software Engineer", "city": city,
            "since": since}


def env_for(extra: dict) -> dict:
    e = dict(os.environ)
    e.update({"PYTHONPATH": str(REPO), "PYTHONIOENCODING": "utf-8",
              "BH_RUNTIME_DIR": str(RUNTIME), "BH_PROFILE_DIRS": str(PROFILE),
              "BU_CDP_URL": "", "BU_CDP_WS": "", "BU_NAME": "dashboard"})
    e.update({k: str(v) for k, v in extra.items()})
    return e


def input_alive() -> bool:
    """A live TCP port is not a live renderer. Windows occlusion throttling stops the
    compositor acknowledging Input.* while leaving the debug port open, which looked
    exactly like every site failing at once."""
    probe = ("import sys\n"
             "try:\n"
             "    cdp('Input.dispatchMouseEvent', {'type':'mouseMoved','x':1,'y':1,"
             "'button':'none'}, timeout=5.0)\n"
             "    print('ALIVE')\n"
             "except Exception as e:\n"
             "    print('DEAD', type(e).__name__)\n")
    try:
        p = subprocess.run([sys.executable, "-m", "harness.cli.main", "-"], input=probe,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=str(REPO), env=env_for({}), timeout=45, check=False)
        return "ALIVE" in (p.stdout or "")
    except Exception:  # noqa: BLE001 — a dead probe is a dead browser, whatever the cause
        return False


def launch_chrome() -> None:
    PROFILE.mkdir(parents=True, exist_ok=True)
    (PROFILE / "DevToolsActivePort").unlink(missing_ok=True)
    if not CHROME:
        return
    flags = ["--disable-features=CalculateNativeWinOcclusion"] if sys.platform == "win32" else []
    subprocess.Popen(
        [CHROME, f"--user-data-dir={PROFILE}", "--remote-debugging-port=0",
         "--no-first-run", "--no-default-browser-check", "--window-size=1250,1000",
         *flags, "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(150):
        if (PROFILE / "DevToolsActivePort").exists():
            break
        time.sleep(0.1)
    time.sleep(0.9)


def ensure_chrome() -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    PROFILE.mkdir(parents=True, exist_ok=True)
    pf = PROFILE / "DevToolsActivePort"
    up = False
    if pf.exists():
        try:
            port = int(pf.read_text(encoding="utf-8").splitlines()[0])
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                up = True
        except (OSError, ValueError, IndexError):
            pf.unlink(missing_ok=True)
    if up and input_alive():
        return
    if up:                       # port open but the renderer is not answering input
        with LOCK:
            STATE["message"] = "browser was throttled — relaunching"
        for p in ("Local State", "SingletonLock"):
            (PROFILE / p).unlink(missing_ok=True)
    launch_chrome()


def run_bh(script: Path, extra: dict, timeout: float = 240.0) -> str:
    p = subprocess.run([sys.executable, "-m", "harness.cli.main", "-"],
                       input=script.read_text(encoding="utf-8"), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", cwd=str(REPO),
                       env=env_for(extra), timeout=timeout, check=False)
    return (p.stdout or "") + (p.stderr or "")


def do_search(prompt: str, count: int) -> None:
    parsed = parse_prompt(prompt)
    with LOCK:
        STATE.update(query=prompt, parsed=parsed, phase="searching",
                     message="opening joblens…", jobs=[])
    try:
        ensure_chrome()
        out = run_bh(HERE / "search_job.py",
                     {"DASH_TITLE": parsed["title"], "DASH_CITY": parsed["city"],
                      "DASH_SINCE": parsed["since"], "DASH_COUNT": count}, timeout=600)
        line = next((ln for ln in out.splitlines() if ln.startswith("JOBS ")), "")
        jobs = json.loads(line[5:]) if line else []
        for i, j in enumerate(jobs):
            j.update(idx=i, status="pending", filled=0, attempted=0, shot="", note="",
                     error="", fields_seen=0)
        with LOCK:
            STATE.update(jobs=jobs, phase="ready" if jobs else "idle",
                         message=f"{len(jobs)} applications found" if jobs
                         else "no reachable forms — " + out[-240:])
    except Exception as e:  # noqa: BLE001 — surfaced in the UI, never raised at a user
        with LOCK:
            STATE.update(phase="idle", message=f"search failed: {type(e).__name__}: {e}")


def fill_one(job: dict) -> None:
    with LOCK:
        job["status"] = "running"
    shot = SHOTS / f"{job['idx']:02d}.png"      # PNG: lossless text on flat UI shots
    try:
        out = run_bh(HERE / "fill_job.py",
                     {"DASH_IDX": job["idx"], "DASH_URL": job["apply_url"],
                      "DASH_SHOT": str(shot),
                      "DASH_APPLICANT": json.dumps(STATE["applicant"], ensure_ascii=False)},
                     timeout=200)
        line = next((ln for ln in out.splitlines() if ln.startswith("RESULT ")), "")
        r = json.loads(line[7:]) if line else {"error": out[-200:] or "no result"}
        with LOCK:
            job.update({k: r.get(k, job.get(k)) for k in
                        ("filled", "attempted", "shot", "note", "error", "fields_seen",
                         "submit_labels", "failures", "url_final")})
            job["status"] = "done" if r.get("filled", 0) > 0 else "failed"
    except Exception as e:  # noqa: BLE001 — one bad application must not stop the run
        with LOCK:
            job.update(status="failed", error=f"{type(e).__name__}: {str(e)[:120]}")


def do_run() -> None:
    with LOCK:
        STATE.update(phase="running", message="filling applications…")
        jobs = list(STATE["jobs"])
    SHOTS.mkdir(parents=True, exist_ok=True)
    ensure_chrome()
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
        list(pool.map(fill_one, jobs))
    with LOCK:
        done = sum(1 for j in STATE["jobs"] if j["status"] == "done")
        STATE.update(phase="done",
                     message=f"{done}/{len(STATE['jobs'])} filled — nothing submitted")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, (HERE / "index.html").read_bytes(),
                              "text/html; charset=utf-8")
        if self.path.startswith("/api/state"):
            with LOCK:
                return self._send(200, json.dumps(STATE, ensure_ascii=False).encode())
        if self.path.startswith("/shots/"):
            f = SHOTS / os.path.basename(self.path[7:].split("?")[0])
            if f.exists():
                return self._send(200, f.read_bytes(),
                                  mimetypes.guess_type(str(f))[0] or "image/png")
        return self._send(404, b"{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/search":
            with LOCK:
                if STATE["phase"] in ("searching", "running"):
                    return self._send(409, b'{"error":"busy"}')
            threading.Thread(target=do_search, daemon=True, args=(
                payload.get("prompt", ""), int(payload.get("count", 10)))).start()
            return self._send(200, b'{"ok":true}')
        if self.path == "/api/run":
            with LOCK:
                if STATE["phase"] == "running" or not STATE["jobs"]:
                    return self._send(409, b'{"error":"nothing to run"}')
            threading.Thread(target=do_run, daemon=True).start()
            return self._send(200, b'{"ok":true}')
        if self.path == "/api/applicant":
            with LOCK:
                STATE["applicant"].update({k: str(v) for k, v in payload.items()})
            return self._send(200, b'{"ok":true}')
        if self.path == "/api/reset":
            with LOCK:
                for j in STATE["jobs"]:
                    j.update(status="pending", filled=0, shot="", error="")
                STATE.update(phase="ready", message="reset")
            return self._send(200, b'{"ok":true}')
        return self._send(404, b"{}")


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(RUNTIME, ignore_errors=True)
    RUNTIME.mkdir(parents=True, exist_ok=True)
    if not CHROME:
        print("warning: no Chrome found; set BH_CHROME", file=sys.stderr)
    print(f"dashboard: http://127.0.0.1:{PORT}   (work dir: {WORK})")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
