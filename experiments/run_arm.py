"""Run experiment arms of the 100-posting application dry run, one after another.

Each arm: kill any `exp` daemon, launch a scratch Chrome (headless unless the arm says
headed), run tools/collect_job_form_telemetry.py through `bh` with the arm's environment,
then kill daemon and Chrome. Resumable: an arm whose results.json exists is skipped.

    python run_arm.py schedule.json            # run every arm in order
    python run_arm.py schedule.json --only E01  # arms whose name starts with E01
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
V2 = HERE.parent / "v2"
BH = V2 / ".venv" / "Scripts" / "bh.exe"
PY = V2 / ".venv" / "Scripts" / "python.exe"
COLLECTOR = V2 / "tools" / "collect_job_form_telemetry.py"
OUT_ROOT = HERE / "out"
DAEMON = "exp"
SCRATCH = Path(os.environ.get("EXP_SCRATCH") or (HERE / "chrome"))

sys.path.insert(0, str(V2 / "tests" / "live"))
sys.path.insert(0, str(V2))
import _browser


def kill_daemon(name: str = DAEMON) -> int:
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "
          f"'harness\\.cli\\.main daemon {name}(\\s|$)' }} | ForEach-Object {{ "
          "Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }")
    ps = ps.replace("{{", "{").replace("}}", "}")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True)
    pids = [p for p in r.stdout.split() if p.strip().isdigit()]
    # the port file would otherwise make the next client ping a dead endpoint for a while
    from harness.core import ipc
    try:
        ipc.port_path(name).unlink()
    except OSError:
        pass
    return len(pids)


def run_arm(arm: dict, *, force: bool = False) -> dict:
    name = arm["name"]
    out = OUT_ROOT / arm.get("exp", "misc") / name
    if ((out / "results.json").exists() or (out / "replay_results.json").exists()) and not force:
        return {"name": name, "skipped": True}
    out.mkdir(parents=True, exist_ok=True)
    profile = SCRATCH / f"{name}-{int(time.time())}"
    profile.mkdir(parents=True, exist_ok=True)
    headed = bool(arm.get("headed"))
    os.environ["BH_HEADLESS"] = "" if headed else "1"
    kill_daemon()
    _browser.launch(profile, window=arm.get("window", "1200,800"))
    env = {**os.environ,
           "BU_NAME": DAEMON, "BH_PROFILE_DIRS": str(profile),
           "BH_JOURNAL": str(out / "journal.jsonl"), "BH_CDP_TRACE": "1",
           "BH_APPLICATION_INPUT": str(V2 / arm.get("input", "jobs_run100.json")),
           "BH_APPLICATION_TELEMETRY_OUT": str(out),
           "BH_APPLICATION_WORKERS": str(arm.get("workers", 10)),
           "BH_APPLICATION_WORKER_LIMIT": str(arm.get("worker_limit", max(10, int(arm.get("workers", 10))))),
           "PYTHONIOENCODING": "utf-8",
           **{k: str(v) for k, v in (arm.get("env") or {}).items()}}
    env.pop("BH_HEADLESS", None)
    meta = {"name": name, "exp": arm.get("exp"), "headed": headed, "workers": arm.get("workers", 10),
            "env": arm.get("env") or {}, "profile": str(profile), "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
    t0 = time.time()
    try:
        with open(out / "run.log", "w", encoding="utf-8") as log:
            script = V2 / arm.get("script", "tools/collect_job_form_telemetry.py")
            proc = subprocess.run([str(BH)], input=script.read_text(encoding="utf-8"), cwd=str(V2), env=env,
                                  stdout=log, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                  timeout=float(arm.get("timeout_s", 1800)))
        meta["returncode"] = proc.returncode
    except subprocess.TimeoutExpired:
        meta["returncode"] = "timeout"
    finally:
        meta["wall_s"] = round(time.time() - t0, 1)
        kill_daemon()
        try:
            _browser.kill(profile)
        except Exception as error:  # noqa: BLE001
            meta["chrome_kill_error"] = str(error)[:200]
        # A scratch profile is ~280 MB of cache after one arm; 62 of them were 17 GB.
        import shutil
        for _attempt in range(5):
            try:
                shutil.rmtree(profile)
                break
            except OSError:
                time.sleep(1.0)
        meta["profile_removed"] = not profile.exists()
    meta["ended"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    meta["ok"] = (out / "results.json").exists() or (out / "replay_results.json").exists()
    (out / "arm.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return meta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("schedule")
    ap.add_argument("--only", default="")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    arms = json.loads(Path(args.schedule).read_text(encoding="utf-8"))
    for arm in arms:
        if args.only and not arm["name"].startswith(args.only):
            continue
        meta = run_arm(arm, force=args.force)
        print(json.dumps({k: meta.get(k) for k in ("name", "skipped", "ok", "wall_s", "returncode")}), flush=True)


if __name__ == "__main__":
    main()
