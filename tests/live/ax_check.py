"""`ax()` against real Chrome — the accessible name, computed by the browser.

`snapshot()` names an element by a chain, `aria-label || innerText || value ||
placeholder || name`. Measured against Chrome on this fixture it named three of nine: it
returned nothing for `aria-labelledby`, `<label for>`, a wrapping `<label>`, `title` and
an image's `alt`, and named a radio `"yes"` — its form value, which reads like an answer.

Every control below is named by a mechanism that chain does not consult, so a regression
here is the whole point of the helper going away. Nothing exercises the accessibility
tree except a real browser.

    BH_HEADLESS=1 uv run python tests/live/ax_check.py
"""
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "tests" / "live")]

PAGE = """<!doctype html><meta charset=utf-8><title>Naming</title>
<span id=lbl>Postal code</span><input id=a aria-labelledby=lbl>
<label for=b>Date of birth</label><input id=b>
<label>Nationality <input id=c></label>
<input id=d title="Mobile number">
<button id=e><img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" alt="Submit application"></button>
<label for=g>Terms</label><input id=g type=checkbox checked>
<label for=h>Country</label><select id=h><option>CH</option><option>DE</option></select>
<button id=i disabled>Closed</button>
<a href=#x aria-expanded=true>More options</a>"""


class _Site(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


site = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
threading.Thread(target=site.serve_forever, daemon=True).start()
scratch = Path(tempfile.mkdtemp(prefix="bh-ax-"))
os.environ.setdefault("BH_HEADLESS", "1")
os.environ["BH_PROFILE_DIRS"] = str(scratch)

import _browser

_browser.launch(scratch, window="1100,900")
ok = fail = 0


def check(label, condition, got=""):
    global ok, fail
    if condition:
        ok += 1
        print(f"  PASS  {label:<58} {got}")
    else:
        fail += 1
        print(f"  FAIL  {label:<58} {got}")


try:
    from harness.ops import forms
    from harness.session import Session

    session = Session("axcheck")
    tab = session.tab()
    tab.goto(f"http://127.0.0.1:{site.server_port}/")

    rows = tab.ax(limit=40)
    named = {row["name"]: row for row in rows if row["name"]}

    for label, mechanism in (("Postal code", "aria-labelledby"),
                             ("Date of birth", "label for"),
                             ("Nationality", "a wrapping label"),
                             ("Mobile number", "title"),
                             ("Submit application", "an image's alt")):
        check(f"named by {mechanism}", label in named,
              label if label in named else "MISSING")

    check("the role is computed, not the tag name",
          named.get("Country", {}).get("role") == "combobox",
          str(named.get("Country", {}).get("role")))
    check("platform state travels with the row",
          named.get("Terms", {}).get("checked") == "true")
    check("a disabled control says so", named.get("Closed", {}).get("disabled") is True)
    check("so does an expanded one", named.get("More options", {}).get("expanded") is True)

    # The reason rows carry ordinary refs: a ref is looked up in about twenty places, and
    # none of them should have to learn where it came from.
    ref = named["Date of birth"]["ref"]
    check("rows carry an ordinary snapshot ref", isinstance(ref, str) and ref.startswith("e"),
          str(ref))
    check("set_value drives one with no change to set_value",
          forms.set_value(tab, ref, "1990-01-01").ok)
    check("and the value landed",
          any(v.get("value") == "1990-01-01" for v in tab.form_values()["values"]))
    check("click_ref drives one too",
          isinstance(tab.click_ref(named["Submit application"]["ref"]), dict))

    check("filtering by name", [r["name"] for r in tab.ax("Nationality")] == ["Nationality"])
    check("filtering by role", all(r["role"] == "button" for r in tab.ax(role="button")))
    check("one pattern spans several names, as a real alternation must",
          {r["name"] for r in tab.ax(pattern=r"(postal|geburt|date of)")}
          == {"Postal code", "Date of birth"})
    check("exclude drops a match", tab.ax(pattern=r"date", exclude=r"birth") == [])
    check("refs=False skips the binding round trips",
          all(r["ref"] is None for r in tab.ax(limit=5, refs=False)))
finally:
    _browser.kill(scratch)
    site.shutdown()

print(f"\n{ok}/{ok + fail} passed")
sys.exit(1 if fail else 0)
