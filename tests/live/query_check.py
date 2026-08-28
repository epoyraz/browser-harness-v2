"""find / extract / form_values against real Chrome.

These three are almost entirely JavaScript, and no unit test executes JavaScript — the
fake browser answers `Runtime.evaluate` without running it. A previous split of
`_SCHEMA_JS` shipped a raw-string bug that every unit test passed and only a live fixture
caught, so a helper whose body is JS is not covered until it runs here.

    BH_HEADLESS=1 uv run python tests/live/query_check.py
"""
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests" / "live")]

PAGE = """<!doctype html><meta charset=utf-8><title>Shop</title>
<a href="/a">Alpha link</a><a href="/b">Beta link</a>
<a href="/j1">Auf diese Stelle bewerben</a>
<a href="/j2">Postuler maintenant</a>
<a href="/newsletter">Job-Alert abonnieren</a>
<a href="/apply/privacy">Candidates Privacy Notice</a>
<a href="/j3">We are always happy to receive an application from candidates who bewerben themselves early</a>
<button aria-label="Add to basket">Buy</button>
<button>Unrelated</button>
<ul>
 <li class=card><h3>Widget</h3><span class=price>9.90</span><a href="/w">open</a></li>
 <li class=card><h3>Gadget</h3><span class=price>19.90</span><a href="/g">open</a></li>
 <li class=card><h3>Doohickey</h3><span class=price>4.50</span><a href="/d">open</a></li>
</ul>
<form><input id=n name=fullname placeholder="Full name">
<input id=p type=password name=pw>
<select id=s name=country><option>CH</option><option>DE</option></select>
<input id=c type=checkbox name=terms></form>"""

class S(BaseHTTPRequestHandler):
    def do_GET(self):
        b = PAGE.encode(); self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def log_message(self,*a): pass

site = ThreadingHTTPServer(("127.0.0.1",0), S)
threading.Thread(target=site.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{site.server_port}/"
scratch = Path(tempfile.mkdtemp(prefix="bh-nh-"))
os.environ.setdefault("BH_HEADLESS","1"); os.environ["BH_PROFILE_DIRS"]=str(scratch)
import _browser

_browser.launch(scratch, window="1200,900")
ok = fail = 0
def check(label, cond, got=""):
    global ok, fail
    if cond: ok += 1; print(f"  PASS  {label:<58} {got}")
    else:    fail += 1; print(f"  FAIL  {label:<58} {got}")
try:
    from harness.core.outcome import HarnessError
    from harness.session import Session
    s = Session("nhcheck"); t = s.tab(); t.goto(base)

    rows = t.find("basket")
    check("find matches on the accessible name", len(rows)==1 and rows[0]["tag"]=="button", rows[0]["name"] if rows else "")
    check("find reports which link named it", rows and rows[0].get("name_source")=="aria", rows[0].get("name_source") if rows else "")
    check("find rows are snapshot rows a click takes", bool(rows and rows[0]["ref"]), rows[0]["ref"] if rows else "")
    anchors = PAGE.count("<a href=")
    check("find filters by tag", len(t.find(tag="a", limit=99))==anchors,
          f"{anchors} anchors in the fixture")
    check("find honours its limit", len(t.find(tag="a", limit=1))==1)
    check("find on nothing is empty, not an error", t.find("no-such-control")==[])

    # The real query, taken from applications/document.py: one alternation across seven
    # languages, a disqualifying pattern, and a cap that rejects prose containing the verb.
    APPLY = r"(bewerb|apply|postul|candidat|candida|sollicit|solicit|aplicar|ansok)"
    NOT_APPLY = r"(newsletter|job.?alert|abonnier|privacy|notice)"
    hits = t.find(pattern=APPLY, exclude=NOT_APPLY, max_len=60, tag="a")
    names = sorted(h["name"] for h in hits)
    check("pattern matches across languages a substring cannot",
          names == ["Auf diese Stelle bewerben", "Postuler maintenant"], str(names))
    check("exclude drops the newsletter and the privacy link",
          not any("abonnier" in n or "Privacy" in n for n in names))
    check("max_len rejects the sentence that merely contains the verb",
          not any(len(n) > 60 for n in names))
    check("without exclude, the decoys come back",
          len(t.find(pattern=APPLY, max_len=60, tag="a")) > len(hits))
    try:
        t.find(pattern="(unclosed")
        check("an uncompilable pattern is refused, not silently empty", False)
    except HarnessError as e:
        check("an uncompilable pattern is refused, not silently empty", True,
              e.outcome.cls.value)

    out = t.extract("a")
    check("extract defaults to text+href",
          out["returned"]==anchors and out["rows"][0]["text"]=="Alpha link",
          f'{out["returned"]} rows')
    check("extract rows carry a ref to act on", all(r.get("ref") for r in out["rows"]))
    cards = t.extract("li.card", {"title":"h3","price":".price","url":"a@href"})
    check("extract maps relative selectors and attributes",
          [r["title"] for r in cards["rows"]]==["Widget","Gadget","Doohickey"], str([r["price"] for r in cards["rows"]]))
    check("extract reads an attribute with @", cards["rows"][0]["url"].endswith("/w"), cards["rows"][0]["url"])
    capped = t.extract("li.card", {"title":"h3"}, limit=2)
    check("extract says the ceiling bit", capped["returned"]==2 and capped["matched"]==3 and capped["truncated"],
          f'returned={capped["returned"]} matched={capped["matched"]}')
    missing = t.extract("li.card", {"title":"h3","nope":".absent"})
    check("an absent field is null, so every row has the same keys",
          all("nope" in r and r["nope"] is None for r in missing["rows"]))
    try:
        t.extract("li.card[")
        check("a bad selector is refused, not silently empty", False)
    except HarnessError as e:
        check("a bad selector is refused, not silently empty", True, e.outcome.cls.value)

    from harness.ops import forms
    name_ref = t.find("Full name")[0]["ref"]
    forms.set_value(t, name_ref, "Ada Lovelace")
    # The redaction only means something once there is a secret to redact; an empty
    # password reads "" in both this helper and an action's consequence, correctly.
    forms.set_value(t, t.find("pw")[0]["ref"], "hunter2")
    raw = t.form_values()["values"]
    vals = {v.get("name") or v.get("ref"): v for v in raw}
    check("form_values reads the whole form back", len(vals)>=4, f'{len(vals)} controls')
    name = next(v for v in vals.values() if v.get("value")=="Ada Lovelace")
    check("form_values returns what was written", name["value"]=="Ada Lovelace")
    pw = next(v for v in vals.values() if v.get("type")=="password")
    check("a password reads [set], never the secret", pw["value"]=="[set]", pw["value"])
    check("validity travels with the value", "valid" in name)
finally:
    _browser.kill(scratch); site.shutdown()
print(f"\n{ok}/{ok+fail} passed")
sys.exit(1 if fail else 0)
