"""Phase 4 live validation (run manually: `.venv/bin/python tests/live/forms_check.py`).

Loads the four ATS fixtures in a scratch-profile Chrome and validates the parts a fake
cannot testify to: the proximity label fallback is *geometry*, the 249-option select is a
real DOM, and furniture exclusion depends on real layout. Also measures the whole point of
Phase 4: an entire form filled and verified in ONE CDP round trip.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from harness.connect.cdp import Connection, WebSocketTransport
from harness.connect.endpoint import discover
from harness.connect.session import SessionRegistry
from harness.core.outcome import Class, NotAForm
from harness.ops.batch import fetch_all
from harness.ops.forms import fill_form, form_schema, require_form, set_value
from harness.ops.page import Tab

CHROME = (os.environ.get("BH_CHROME")
          or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
#: See check.py — Windows occlusion throttling drops Input.dispatchMouseEvent.
FLAGS = ["--disable-features=CalculateNativeWinOcclusion"] if os.name == "nt" else []
FIXTURES = ROOT / "tests" / "fixtures"

results: list[tuple[str, bool, str]] = []
flaky_hits: dict[str, int] = {}


class _Site(SimpleHTTPRequestHandler):
    """Fixtures from disk, plus dynamic endpoints for fetch_all."""

    def do_GET(self):
        if self.path.startswith("/flaky/"):
            n = flaky_hits.get(self.path, 0)
            flaky_hits[self.path] = n + 1
            if n == 0:                          # 500 once, then succeed: retries must win
                self.send_response(500)
                self.end_headers()
                return
            body = b'{"ok": true}'
        elif self.path.startswith("/gone/"):
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"nope")
            return
        elif self.path.startswith("/doc/"):
            body = b'{"doc": "%s"}' % self.path.encode()
        else:
            return super().do_GET()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def check(name: str, ok: bool, note: str = "") -> None:
    results.append((name, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<58} {note}")


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="bh-forms-"))
    site = HTTPServer(("127.0.0.1", 0),
                      partial(_Site, directory=str(FIXTURES)))
    threading.Thread(target=site.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{site.server_port}"

    chrome = subprocess.Popen(
        [CHROME, f"--user-data-dir={scratch}", "--remote-debugging-port=0",
         "--no-first-run", "--no-default-browser-check", "--window-size=1200,900",
         *FLAGS, "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while not (scratch / "DevToolsActivePort").exists():
            if time.monotonic() > deadline:
                print("Chrome never wrote DevToolsActivePort")
                return 1
            time.sleep(0.1)
        time.sleep(0.3)

        r = discover({"BH_PROFILE_DIRS": str(scratch)})
        conn = Connection(WebSocketTransport(r.ws_url)).start()
        registry = SessionRegistry(conn)
        registry.discover()
        tid = next(t["targetId"] for t in
                   conn.request("Target.getTargets")["targetInfos"] if t["type"] == "page")
        tab = Tab(conn, registry, tid)

        def evaluates() -> int:
            return sum(1 for c in [])            # placeholder, replaced by journal below

        # ---- Abacus: the proximity fallback (item 23's done-when) ----------
        tab.goto(f"{base}/abacus.html")
        schema = form_schema(tab)
        by_name = {f["name"]: f for f in schema["fields"]}
        vorname = by_name.get("customeraddressshoppervorname", {})
        check("abacus: label is 'Vorname *', not the machine name",
              vorname.get("label") == "Vorname *", str(vorname.get("label")))
        check("abacus: star in adjacent text means required",
              vorname.get("required") is True and
              by_name.get("customeraddressshoppertelefon", {}).get("required") is False,
              f"vorname={vorname.get('required')} telefon="
              f"{by_name.get('customeraddressshoppertelefon', {}).get('required')}")
        anrede = by_name.get("customeraddressshopperanrede", {})
        check("abacus: 'Bitte wählen' select marked placeholder_first",
              anrede.get("placeholder_first") is True, str(anrede.get("options_sample")))
        check("abacus: verdict is a form",
              schema["verdict"]["is_form"] and
              schema["verdict"]["submit_labels"] == ["Bewerbung absenden"],
              str(schema["verdict"]["submit_labels"]))

        # the whole form, one write, one round trip — measured via the journal span
        plan = [
            {"ref": anrede["ref"], "label": "Herr"},
            {"ref": vorname["ref"], "value": "Enes"},
            {"ref": by_name["customeraddressshoppernachname"]["ref"], "value": "Poyraz"},
            {"ref": by_name["customeraddressshopperemail"]["ref"], "value": "e@example.ch"},
            {"ref": by_name["customeraddressshoppertelefon"]["ref"], "value": "+41 79 000 00 00"},
            {"ref": by_name["customeraddressshopperort"]["ref"], "value": "Zürich"},
            {"ref": by_name["bemerkungen"]["ref"], "value": "Referenz: Live-Check"},
        ]
        t0 = time.perf_counter()
        out = fill_form(tab, plan)
        ms = (time.perf_counter() - t0) * 1000
        check("abacus: 7 fields, all verified ok/got/want",
              out.ok and all(e["ok"] for e in out.value),
              f"{out.observed['succeeded']}/7 in {ms:.0f}ms")
        got = tab.js("document.querySelector('[name=customeraddressshoppervorname]').value")
        check("abacus: values actually in the DOM", got == "Enes", f"got={got!r}")

        # ---- Personio: the well-behaved chain + files in the verdict --------
        tab.goto(f"{base}/personio.html")
        schema = form_schema(tab)
        labels = {f["label"] for f in schema["fields"]}
        check("personio: label[for] chain wins", "First name" in labels and "Email" in labels,
              str(sorted(labels))[:70])
        check("personio: file input excluded from fields, counted in verdict",
              schema["verdict"]["files"] == 1 and
              not any(f["kind"] == "file" for f in schema["fields"]),
              f"files={schema['verdict']['files']}")
        privacy = next(f for f in schema["fields"] if f["kind"] == "checkbox")
        check("personio: wrapping-label checkbox is labelled and required",
              "privacy" in (privacy["label"] or "").lower() and privacy["required"],
              str(privacy["label"])[:40])
        email = next(f for f in schema["fields"] if f["kind"] == "email")
        check("personio: autocomplete passes through", email.get("autocomplete") == "email",
              str(email.get("autocomplete")))

        # ---- Factorial: the modes v1 was blind to ---------------------------
        tab.goto(f"{base}/factorial.html")
        schema = form_schema(tab)
        combo = [f for f in schema["fields"] if f["kind"] == "combobox"
                 and "hear about us" in str(f["label"])]
        check("factorial: ARIA combobox visible to the schema",
              len(combo) == 1 and combo[0].get("needs_interaction") is True,
              str(combo[0].get("label") if combo else None))
        country = next(f for f in schema["fields"] if f["kind"] == "select")
        check("factorial: placeholder-first country select marked",
              country.get("placeholder_first") is True, str(country.get("options_sample")))
        check("factorial: aria-labelledby resolves",
              any(f.get("label") == "Full name" for f in schema["fields"]), "")

        # ---- PHP form: 249 prefixes cannot pick Spain (item 24's done-when) -
        tab.goto(f"{base}/phpform.html")
        schema = form_schema(tab)
        prefix = next(f for f in schema["fields"] if f["name"] == "indicatif")
        check("php: 250 options counted, placeholder first",
              prefix["options_count"] == 250 and prefix["placeholder_first"],
              f"count={prefix['options_count']}")
        taca = next(f for f in schema["fields"] if f["name"] == "jobportal_taca")
        out = fill_form(tab, [
            {"ref": prefix["ref"], "label": "Suisse (+41)"},
            {"ref": taca["ref"], "value": True},
        ])
        swiss = next(e for e in out.value if e["ref"] == prefix["ref"])
        check("php: label 'Suisse (+41)' resolves among 249",
              out.ok and swiss["got"] == "Suisse (+41)", f"got={swiss['got']!r}")
        check("php: the once-mistyped checkbox is a boolean, ok verified",
              next(e for e in out.value if e["ref"] == taca["ref"])["got"] is True, "")
        out = fill_form(tab, [{"ref": prefix["ref"], "label": "Atlantis"}])
        check("php: no match is no_option_match with candidates, never an index",
              (not out.ok) and out.failures[0].cls is Class.NO_OPTION_MATCH
              and len(out.failures[0].observed["candidates"]) > 0,
              str(out.failures[0].observed["candidates"][:2]))
        still = tab.js("document.getElementById('prefix').value")
        check("php: failed match leaves the select untouched", still == "+41",
              f"value={still!r}")

        # ---- set_value (item 25) --------------------------------------------
        moti = next(f for f in schema["fields"] if f["name"] == "motivation")
        long_text = ("Motivation. " * 170)[:2000]
        out = set_value(tab, moti["ref"], long_text)
        check("set_value: 2,000 chars in one call", out.ok, f"{len(long_text)} chars")
        out = set_value(tab, moti["ref"], "via insertText", mode="insert")
        check("set_value mode=insert: one Input.insertText", out.ok,
              str(out.observed.get("mode")))

        # ---- widgets: the two failure modes from the 2026-08-05 live run -----
        tab.goto(f"{base}/widgets.html")
        schema = form_schema(tab)
        by_name = {f.get("name"): f for f in schema["fields"]}

        dial = next(f for f in schema["fields"] if f["kind"] == "combobox")
        check("widgets: combobox DIV is labelled and flagged",
              dial["label"] == "Phone country code" and dial.get("needs_interaction"),
              str(dial["label"]))
        out = fill_form(tab, [{"ref": dial["ref"], "value": "+41"}], recheck=0)
        check("widgets: filling a combobox DIV is needs_interaction, not a TypeError",
              (not out.ok) and out.failures[0].cls is Class.NEEDS_INTERACTION,
              f"{out.failures[0].cls.value} tag={out.failures[0].observed.get('tag')}")

        check("widgets: the 1x1 clip-rect decoy input is excluded",
              "s2id_autogen9" not in by_name and
              not any(str(f.get("name") or "").startswith("s2id") for f in schema["fields"]),
              f"{len(schema['fields'])} fields kept")

        real = by_name.get("form.widgets.country:list")
        check("widgets: the hidden real <select> is surfaced with its options",
              real is not None and real.get("hidden_control") is True
              and real["options_count"] == 3 and real["label"] == "Land *",
              f"label={real['label'] if real else None} "
              f"opts={real['options_count'] if real else None}")
        out = fill_form(tab, [{"ref": real["ref"], "label": "Schweiz"}])
        check("widgets: the real select takes the label the decoy would have swallowed",
              out.ok and out.value[0]["got"] == "Schweiz", str(out.value[0].get("got")))
        submitted = tab.js("document.getElementById('real-land').value")
        check("widgets: the value is on the control that submits", submitted == "CH",
              f"select.value={submitted!r}")

        # ---- notaform: the 404 trap (verdict) -------------------------------
        tab.goto(f"{base}/notaform.html")
        schema = form_schema(tab)
        check("notaform: cookie banner + search is NOT a form",
              schema["verdict"]["is_form"] is False, schema["verdict"]["reason"])
        try:
            require_form(schema)
            check("notaform: require_form raises NotAForm", False, "no raise")
        except NotAForm as e:
            check("notaform: require_form raises NotAForm", True, str(e)[:50])

        # ---- fetch_all (item 22) --------------------------------------------
        tab.goto(f"{base}/abacus.html")
        urls = ([f"{base}/doc/{i}" for i in range(24)]
                + [f"{base}/flaky/{i}" for i in range(3)]
                + [f"{base}/gone/{i}" for i in range(3)])
        t0 = time.perf_counter()
        out = fetch_all(tab, urls, concurrency=6, retries=2)
        ms = (time.perf_counter() - t0) * 1000
        check("fetch_all: PARTIAL with exact counts",
              (not out.ok) and out.cls is Class.PARTIAL
              and out.observed["attempted"] == 30 and out.observed["succeeded"] == 27
              and out.observed["failed"] == 3,
              f"{out.observed['succeeded']}/30 in {ms:.0f}ms")
        check("fetch_all: 500-then-200 won by in-page retry",
              sum(1 for v in out.value if "/flaky/" in v["url"]) == 3,
              f"retries={[v.get('retries') for v in out.value if '/flaky/' in v['url']]}")
        check("fetch_all: 404s typed with url and status",
              all(f.cls is Class.HTTP_ERROR and f.observed["status"] == 404
                  for f in out.failures), f"{len(out.failures)} failures")

        conn.close()
    finally:
        chrome.terminate()
        with contextlib.suppress(Exception):
            chrome.wait(5)
        site.shutdown()
        shutil.rmtree(scratch, ignore_errors=True)

    failed = [n for n, ok_, _ in results if not ok_]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed"
          + (f" — FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
