"""Experiments that need a browser but not the application workflow.

E05/E21 digest bytes: what one page read hands the model, per helper and parameter set.
E06 induced disconnect: kill Chrome mid-run; does parallel() stop or burn the queue?

    python cheap_run.py bytes
    python cheap_run.py disconnect --stop 1     # new behaviour
    python cheap_run.py disconnect --stop 0     # old behaviour
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_arm

V2, BH = run_arm.V2, run_arm.BH
OUT = HERE / "out" / "cheap"
DAEMON = "exp2"

BYTES_SCRIPT = r'''
import json, statistics as st
J = lambda x: len(json.dumps(x, default=str))
jobs = json.load(open(r"__JOBS__", encoding="utf-8"))["jobs"]
urls = [ (j.get("apply") or {}).get("direct_url") or j.get("url") for j in jobs ][:100]
def one(url):
    try:
        r = open_page(url, timeout=20)
    except Exception as e:
        return {"url": url, "error": type(e).__name__}
    p = r["page"]
    out = {"url": url, "default": J(r), "text_chars": len(p.get("text") or ""), "blocks": J(p.get("blocks")),
           "links": J(p.get("links")), "n_links": len(p.get("links") or [])}
    try:
        out["minimal"] = J(read_page(max_chars=0, max_links=0))
        out["links8"] = J(read_page(max_chars=6000, max_links=8))
        out["second_read"] = J(read_page())
        out["snapshot"] = J(snapshot())
        out["find_apply"] = J(find(pattern=r"(apply|bewerb|postul|candidat)", max_len=40))
        out["form_schema"] = J(form_schema())
    except Exception as e:
        out["error2"] = type(e).__name__
    return out
recs = parallel(urls, one, workers=5, timeout=600)
rows = [r["value"] for r in recs if r.get("ok") and isinstance(r.get("value"), dict) and "default" in r["value"]]
def med(k):
    v = [r[k] for r in rows if k in r]; return round(st.median(v)) if v else None
summary = {k: med(k) for k in ("default", "minimal", "links8", "second_read", "snapshot", "find_apply", "form_schema", "text_chars", "blocks", "links", "n_links")}
summary["n"] = len(rows); summary["failed"] = len(recs) - len(rows)
summary["p90_default"] = sorted(r["default"] for r in rows)[int(0.9*(len(rows)-1))] if rows else None
json.dump({"summary": summary, "rows": rows}, open(r"__OUT__", "w", encoding="utf-8"), indent=1)
print(json.dumps(summary))
'''

DISCONNECT_SCRIPT = r'''
import json, time
jobs = json.load(open(r"__JOBS__", encoding="utf-8"))["jobs"]
urls = [ (j.get("apply") or {}).get("direct_url") or j.get("url") for j in jobs ][:100]
progress_path = r"__PROGRESS__"
done = 0
def one(url):
    t0 = time.perf_counter()
    r = open_page(url, timeout=20, max_chars=200, max_links=0)
    return {"landed": r["landed"], "ms": round((time.perf_counter()-t0)*1000)}
def progress(n, total, rec):
    open(progress_path, "w").write(str(n))
t0 = time.time()
recs = parallel(urls, one, workers=5, timeout=900, progress=progress)
wall = round(time.time() - t0, 1)
from collections import Counter
classes = Counter(r.get("class") if not r.get("ok") else "ok" for r in recs)
unstarted = sum(1 for r in recs if str(r.get("error","")).endswith("did not start: browser_disconnected"))
attempted_after = sum(1 for r in recs if r.get("class") == "browser_disconnected" and not str(r.get("error","")).startswith("parallel item did not start"))
json.dump({"wall_s": wall, "classes": dict(classes), "unstarted_marked": unstarted, "failed_attempts_after_disconnect": attempted_after,
           "records": recs}, open(r"__OUT__", "w", encoding="utf-8"), indent=1, default=str)
print(json.dumps({"wall_s": wall, "classes": dict(classes), "unstarted_marked": unstarted, "failed_attempts_after_disconnect": attempted_after}))
'''


def _env(profile: Path, extra: dict | None = None) -> dict:
    return {**os.environ, "BU_NAME": DAEMON, "BH_PROFILE_DIRS": str(profile), "PYTHONIOENCODING": "utf-8",
            "BH_HEADLESS": "1", **(extra or {})}


def _launch(name: str) -> Path:
    os.environ["BH_HEADLESS"] = "1"
    run_arm.kill_daemon(DAEMON)
    profile = run_arm.SCRATCH / f"{name}-{int(time.time())}"
    profile.mkdir(parents=True, exist_ok=True)
    run_arm._browser.launch(profile)
    return profile


def _teardown(profile: Path) -> None:
    run_arm.kill_daemon(DAEMON)
    try:
        run_arm._browser.kill(profile)
    except Exception:  # noqa: BLE001
        pass
    import shutil
    for _attempt in range(5):
        try:
            shutil.rmtree(profile)
            break
        except OSError:
            time.sleep(1.0)


def bytes_experiment() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    profile = _launch("bytes")
    try:
        script = BYTES_SCRIPT.replace("__JOBS__", str(V2 / "jobs_run100.json")).replace("__OUT__", str(OUT / "bytes.json"))
        r = subprocess.run([str(BH)], input=script, cwd=str(V2), env=_env(profile), capture_output=True, text=True,
                           encoding="utf-8", timeout=900)
        print(r.stdout[-2000:], r.stderr[-1500:])
    finally:
        _teardown(profile)


def disconnect_experiment(stop: str, kill_at: int = 40) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    profile = _launch(f"disc{stop}")
    progress = OUT / f"disc-progress-{stop}.txt"
    progress.write_text("0")
    out = OUT / f"disconnect-stop{stop}.json"
    try:
        script = (DISCONNECT_SCRIPT.replace("__JOBS__", str(V2 / "jobs_run100.json"))
                  .replace("__PROGRESS__", str(progress)).replace("__OUT__", str(out)))
        proc = subprocess.Popen([str(BH)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                cwd=str(V2), env=_env(profile, {"BH_PARALLEL_STOP_ON_DISCONNECT": stop}), text=True, encoding="utf-8")
        threading.Thread(target=lambda: (proc.stdin.write(script), proc.stdin.close()), daemon=True).start()
        killed_at = None
        t0 = time.time()
        while proc.poll() is None:
            try:
                n = int(progress.read_text() or 0)
            except ValueError:
                n = 0
            if killed_at is None and n >= kill_at:
                killed_at = time.time() - t0
                run_arm._browser.kill(profile)   # Chrome dies -> daemon loses its websocket
            time.sleep(0.5)
            if time.time() - t0 > 900:
                proc.kill(); break
        stdout = proc.stdout.read()
        ended = time.time() - t0
        meta = {"stop": stop, "killed_at_s": round(killed_at, 1) if killed_at else None, "ended_s": round(ended, 1),
                "after_kill_s": round(ended - killed_at, 1) if killed_at else None}
        try:
            summary = {k: v for k, v in json.loads(out.read_text(encoding="utf-8")).items() if k != "records"}
        except Exception:  # noqa: BLE001
            summary = {"error": "no results", "tail": stdout[-1500:]}
        print(json.dumps({**meta, **summary}))
        (OUT / f"disconnect-stop{stop}-meta.json").write_text(json.dumps({**meta, **summary}, indent=1), encoding="utf-8")
    finally:
        _teardown(profile)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("which"); ap.add_argument("--stop", default="1"); ap.add_argument("--kill-at", type=int, default=40)
    a = ap.parse_args()
    if a.which == "bytes":
        bytes_experiment()
    elif a.which == "disconnect":
        disconnect_experiment(a.stop, a.kill_at)
