"""Benchmark: fresh joblens.ch data from a CV, run two ways.

The question is not "how fast is a CDP round trip" — D0 already answered that (0.03% of
wall clock). The question is **how many times the model has to stop and think**, because
that is the term that actually dominates, and the only one collapsing steps reduces.

So the same task runs twice:

  EXPLORATORY  what an agent actually does on an unfamiliar page: open it, look, probe the
               DOM, discover the selector, extract. Each probe is a separate `bh`
               invocation because the model has to *see* one result before writing the
               next script. This is not a strawman — it is a faithful replay of the real
               session that produced jobs.txt, including the two wrong turns.
  COLLAPSED    the same work once the shape is known, in as few invocations as possible.

The delta between them is the value of every "bigger step" primitive in the harness. Run:

    .venv/bin/python tests/bench/joblens.py [--cv PATH] [--think MS]

`--think` is what makes the comparison honest. A scripted benchmark has no model in the
loop, so the real gap between steps is ~0. Pricing each step at a realistic thinking time
is the whole point; the default is deliberately conservative.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from harness.core.bench import render, rollup

CV_DEFAULT = "/Users/rebourne/Desktop/Bewerbung 2026/Lebenslauf – Enes Poyraz.pdf"
URL = "https://joblens.ch/suche"

#: Measured, not guessed: p50 of the real gaps between harness calls in the agent session
#: that produced jobs.txt, via `bh bench --from-transcript` (n=13, mean 15.9s). The old
#: 8000 placeholder understated the delta by roughly half.
THINK_MS_DEFAULT = 15500.0

CHROME = (os.environ.get("BH_CHROME")
          or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def launch_chrome(profile: Path) -> None:
    """A scratch-profile browser of our own.

    Not the daily driver: Chrome M144 grants consent PER WEBSOCKET and refuses a second
    one to an already-authorised browser (D7, measured 0/6), so a benchmark daemon cannot
    share a Chrome that another daemon already holds — the first run of this file failed
    every step on exactly that handshake. A scratch profile also makes the numbers
    reproducible instead of depending on what happens to be open.
    """
    downloads = profile / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    flags = [f"--user-data-dir={profile}", "--remote-debugging-port=0",
             "--no-first-run", "--no-default-browser-check", "--window-size=1280,900",
             f"--download-directory={downloads}", "--disable-features=DefaultBrowserSetting",
             "about:blank"]
    # Launched through `open`, so **launchd** is the responsible process rather than the
    # terminal that started us. This is not cosmetic: launching Chrome as our own child
    # made macOS attribute Chrome's file access to the terminal app, Chrome reached for
    # ~/Downloads on startup, and the resulting TCC prompt appeared *behind* the Chrome
    # window. Unanswered reads as denied, so the terminal lost Desktop access — and this
    # benchmark then could not read its own virtualenv. It happened twice before the
    # cause was clear. The explicit download directory removes the trigger as well.
    subprocess.run(["/usr/bin/open", "-na", CHROME, "--args", *flags],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 25
    while not (profile / "DevToolsActivePort").exists():
        if time.monotonic() > deadline:
            raise RuntimeError("Chrome never wrote DevToolsActivePort")
        time.sleep(0.1)
    time.sleep(0.4)


def kill_chrome(profile: Path) -> None:
    """`open -na` detaches the process, so there is no handle to terminate — find it by
    the scratch profile it was told to use, which no other Chrome can be holding."""
    with contextlib.suppress(Exception):
        subprocess.run(["/usr/bin/pkill", "-f", f"--user-data-dir={profile}"],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)


def _bh(script: str, env: dict, timeout: float = 180) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "harness.cli.main", "-"],
                          input=script, capture_output=True, text=True, check=False,
                          cwd=str(ROOT), env=env, timeout=timeout)


# Every `bh` run is its own process with its own (empty) current tab, so each step after
# the first has to find the page again. That is not benchmark ceremony — it is the tax a
# real agent pays per step, and it belongs in the exploratory column.
#
# Omitting it does not fail loudly, which is the trap: `tab()` falls back to the first
# drivable page, which is Chrome's original about:blank, so the probes quietly answer
# questions about a blank document. The first run of this file scored 60.9x that way.
REATTACH = '''use_tab(next(t for t in targets() if "joblens" in t.get("url",""))["targetId"])
'''


# --------------------------------------------------------------------------
# EXPLORATORY — one invocation per thing the model had to look at.
# --------------------------------------------------------------------------
def exploratory(env: dict, cv: str) -> list[str]:
    steps = [
        # 1. open it and see what loaded
        f'''t = new_tab("{URL}")
import time; time.sleep(2)
print(js("document.title"), len(page_text()))''',
        # 2. a fresh process has no current tab — find it again, then look at the text
        '''print(page_text()[:400])''',
        # 3. what does the search form look like?
        '''print(js("""JSON.stringify([...document.querySelectorAll('input')]
  .map(e => ({t: e.type, ph: e.placeholder||''})))"""))''',
        # 4. drop the CV in
        f'''upload_file("input[type=file]", {cv!r})
import time; time.sleep(6)
print(js("location.href")[:120])''',
        # 5. what shape is a result?
        '''print(js("""(() => {const h=document.querySelector('h2');
  let n=h, c=[]; while(n && c.length<5){c.push(n.tagName); n=n.parentElement;}
  return c.join('>');})()"""))''',
        # 6. where is the link inside the card?
        '''print(js("""(() => {const li=document.querySelector('ol > li');
  const a=li.querySelector('h2 a[href]'); return a ? a.href : 'none';})()"""))''',
        # 7. and which span is the company?
        '''print(js("""(() => {const m=document.querySelector('ol > li h2 + div');
  return [...m.querySelectorAll('span')].map(s=>s.className.slice(0,22)).join(' | ');})()"""))''',
        # 8. finally, extract
        '''import json
raw = js("""(() => JSON.stringify([...document.querySelectorAll('ol > li')]
  .filter(li=>li.querySelector('h2')).map(li => {
    const a=li.querySelector('h2 a[href]');
    const m=li.querySelector('h2 + div');
    const sp=m?[...m.querySelectorAll('span')]:[];
    const comp=sp.find(s=>(s.className||'').includes('uppercase'));
    return {title:a?a.innerText.trim():'', url:a?a.href:'',
            company:comp?comp.innerText.trim():''};})))()""")
print(len(json.loads(raw)))''',
    ]
    return [steps[0]] + [REATTACH + s for s in steps[1:]]


# --------------------------------------------------------------------------
# COLLAPSED — the same work, once the page's shape is known.
# --------------------------------------------------------------------------
def collapsed(env: dict, cv: str) -> list[str]:
    return [f'''
import json, time
t = new_tab("{URL}")
wait_for("input[type=file]", state="present", timeout=20)
upload_file("input[type=file]", {cv!r})
wait_for("ol > li h2", state="visible", timeout=30)
raw = js("""(() => JSON.stringify([...document.querySelectorAll('ol > li')]
  .filter(li => li.querySelector('h2')).map(li => {{
    const a = li.querySelector('h2 a[href]');
    const m = li.querySelector('h2 + div');
    const sp = m ? [...m.querySelectorAll('span')] : [];
    const comp = sp.find(s => (s.className||'').includes('uppercase'));
    const inMeta = new Set(sp);
    return {{
      title: a ? a.innerText.replace(/\\\\s+/g,' ').trim() : '',
      url: a ? a.href : '',
      company: comp ? comp.innerText.trim() : '',
      meta: sp.map(s=>s.innerText.trim()).filter(x=>x.startsWith('\\\\u00b7'))
              .map(x=>x.replace(/^\\\\u00b7\\\\s*/,'')),
      skills: [...li.querySelectorAll('span')].filter(s=>!inMeta.has(s))
              .map(s=>s.innerText.trim()).filter(Boolean)
    }};}})))()""")
jobs = json.loads(raw)
print(json.dumps({{"jobs": len(jobs), "url": js("location.href")[:110],
                  "top": [j["title"][:44] for j in jobs[:5]]}}))
import pathlib
pathlib.Path("/tmp/bench_jobs.json").write_text(json.dumps(jobs, indent=1))
''']


def run(label: str, scripts: list[str], env: dict, journal: Path,
        think: float) -> dict:
    env = {**env, "BH_JOURNAL": str(journal)}
    t0 = time.perf_counter()
    outs = []
    for i, s in enumerate(scripts, 1):
        r = _bh(s, env)
        outs.append((i, r.returncode, (r.stdout or r.stderr).strip()[-90:]))
        if r.returncode != 0:
            print(f"    step {i} FAILED: {(r.stderr or '').strip()[-200:]}")
    wall = (time.perf_counter() - t0) * 1000
    roll = rollup([journal], think_ms=think)
    roll["measured_wall_ms"] = round(wall, 1)
    roll["label"] = label
    for i, rc, tail in outs:
        print(f"    {i:>2}. rc={rc}  {tail[:80]}")
    return roll


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cv", default=CV_DEFAULT)
    ap.add_argument("--think", type=float, default=THINK_MS_DEFAULT)
    a = ap.parse_args()
    if not Path(a.cv).is_file():
        print(f"no CV at {a.cv}")
        return 1

    work = Path(tempfile.mkdtemp(prefix="bh-bench-"))
    profile = Path(tempfile.mkdtemp(prefix="bh-benchprof-"))
    runtime = Path(tempfile.mkdtemp(prefix="bhb-", dir="/tmp"))
    launch_chrome(profile)
    env = {**os.environ, "PYTHONPATH": str(ROOT), "BU_NAME": "bench",
           "BH_RECORD": "0", "BH_RUNTIME_DIR": str(runtime),
           "BH_PROFILE_DIRS": str(profile), "BU_CDP_URL": "", "BU_CDP_WS": ""}
    results = []
    try:
        for label, maker in (("EXPLORATORY", exploratory), ("COLLAPSED", collapsed)):
            print(f"\n=== {label} ===")
            results.append(run(label, maker(env, a.cv), env,
                               work / f"{label.lower()}.jsonl", a.think))
        print()
        for r in results:
            print("=" * 74)
            print(f"{r['label']}   (measured wall {r['measured_wall_ms'] / 1000:,.1f}s "
                  f"excluding think)")
            print("=" * 74)
            for line in render(r, verbose=True):
                print(line)
            print()

        e, c = results
        print("=" * 74)
        print("DELTA")
        print("=" * 74)
        print(f"  steps      {e['steps']:>8} -> {c['steps']:<8} "
              f"({e['steps'] - c['steps']} fewer model decisions)")
        print(f"  CDP        {e['cdp']:>8} -> {c['cdp']:<8}")
        for k in ("think", "connect", "harness", "wait"):
            print(f"  {k:<10} {e['buckets'][k] / 1000:>7,.1f}s -> "
                  f"{c['buckets'][k] / 1000:<7,.1f}s")
        print(f"  {'TOTAL':<10} {e['total_ms'] / 1000:>7,.1f}s -> "
              f"{c['total_ms'] / 1000:<7,.1f}s   "
              f"{e['total_ms'] / max(c['total_ms'], 1):.1f}x")
        (work / "bench.json").write_text(json.dumps(results, indent=2, default=str))
        print(f"\n  raw: {work}/bench.json")
    finally:
        kill_chrome(profile)
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(runtime, ignore_errors=True)
        # journals stay in `work` — the report is the product
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
