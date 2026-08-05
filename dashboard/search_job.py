"""bh script: search joblens, resolve each posting's real apply URL, print JSON.

Printed as one line prefixed with JOBS so the server can find it in the output.
"""
import json
import os
import re
import time

TITLE = os.environ.get("DASH_TITLE", "Software Engineer")
CITY = os.environ.get("DASH_CITY", "")
SINCE = os.environ.get("DASH_SINCE", "")
WANT = int(os.environ.get("DASH_COUNT", "10"))

url = f"https://joblens.ch/?title={TITLE.replace(' ', '+')}"
if CITY:
    url += f"&city={CITY}"
if SINCE:
    url += f"&since={SINCE}"

goto(url, timeout=30.0)
time.sleep(2.0)

CARDS = """
[...document.querySelectorAll('.job-row')].map(r => ({
  company: (r.querySelector('.job-company')||{}).textContent || '',
  date:    (r.querySelector('.job-date')||{}).textContent || '',
  title:   (r.querySelector('.job-title')||{}).textContent || '',
  url:     (r.querySelector('a.job-title')||{}).href || '',
  meta:    [...r.querySelectorAll('.job-meta-item')].map(m => m.textContent.trim())
}))
"""

seen, rows = set(), []
for _ in range(10):
    for c in js(CARDS) or []:
        if c["url"] and c["url"] not in seen:
            seen.add(c["url"])
            rows.append({k: " ".join(str(v).split()) if isinstance(v, str) else v
                         for k, v in c.items()})
    if len(rows) >= WANT * 3:
        break
    scroll(2200)
    time.sleep(0.9)

THIRD = re.compile(
    r"workable\.com|greenhouse\.io|lever\.co|personio\.(de|com)|smartrecruiters\.com|"
    r"myworkdayjobs\.com|successfactors|taleo|jobvite|ashbyhq\.com|recruitee\.com|"
    r"teamtailor\.com|softgarden|ostendis\.com|refline\.ch|umantis\.com|icims\.com|"
    r"prospective\.ch|join\.com|breezy\.hr|oraclecloud\.com|eightfold|avature",
    re.IGNORECASE)

out = []
for r in rows:
    if len(out) >= WANT:
        break
    try:
        goto(r["url"], timeout=25.0)
        time.sleep(0.7)
        apply_url = js(
            "(() => {const a = [...document.querySelectorAll('a')].find(x =>"
            " /bewerben|apply|jetzt/i.test(x.textContent||'')"
            " && x.href && !/joblens\\.ch/.test(x.href)); return a ? a.href : '';})()") or ""
    except Exception:  # noqa: BLE001 — one unreachable posting must not end the search
        apply_url = ""
    if not apply_url:
        continue
    host = re.sub(r"^https?://", "", apply_url).split("/")[0]
    out.append({"company": r["company"], "title": r["title"], "date": r["date"],
                "place": (r["meta"] or [""])[0], "job_url": r["url"],
                "apply_url": apply_url, "host": host,
                "third_party": bool(THIRD.search(apply_url))})

print("JOBS " + json.dumps(out, ensure_ascii=False))
