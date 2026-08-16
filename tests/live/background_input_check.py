"""Every input modality, in a tab that is NOT its window's selected tab.

The renderer silently drops raw Input.* events for background tabs — the CDP call ACKs
either way — and `parallel()` puts every worker but at most one in exactly that state.
This check runs the full input surface against a real background tab and asserts on what
the PAGE observed (fixture-side counters), then repeats the same operations in the
selected tab and asserts that nothing there ever took a fallback path.

Runs the same at HEAD and after the delivery-verified fallbacks: at HEAD the background
half fails, which is the measurement — run it before and after, like improve_bench.

    python tests/live/background_input_check.py
"""
from __future__ import annotations

import shutil
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

from harness.connect.cdp import Connection, WebSocketTransport
from harness.connect.endpoint import discover
from harness.connect.session import SessionRegistry
from harness.core.journal import Journal
from harness.ops.forms import form_schema, select_option, set_value
from harness.ops.page import Tab

FIXTURES = ROOT / "tests" / "fixtures"
results: list[tuple[str, bool, str]] = []


class _Site(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def check(name: str, ok: bool, note: str = "") -> None:
    results.append((name, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<58} {note}")


def drive(tab: Tab, label: str, *, expect_fallback: bool) -> None:
    """The same operations either way; only the expected modality differs."""
    mode = "background" if expect_fallback else "selected"

    # -- typed write (the mode='type' tier) --------------------------------
    tab.goto(f"{tab._base}/bginput.html")
    hidden = tab.js("document.hidden")
    check(f"{label}: tab visibility is what the case needs",
          bool(hidden) is expect_fallback, f"document.hidden={hidden}")
    schema = form_schema(tab)
    field = next(f for f in schema["fields"] if f.get("name") == "name")
    out = set_value(tab, field["ref"], "Enes Poyraz", mode="type")
    got = tab.js("document.getElementById('name').value")
    seen = tab.js("window.counts")
    check(f"{label}: mode='type' writes the value",
          out.ok and got == "Enes Poyraz", f"got={got!r}")
    check(f"{label}: the page's own per-key handlers ran",
          seen["keydown"] >= len("Enes Poyraz"),
          f"keydowns={seen['keydown']} inputs={seen['input']}")

    # -- a named key --------------------------------------------------------
    r = tab.press_key("Escape") or {}           # returns None at pre-fallback HEAD
    esc = tab.js("window.counts.escape")
    check(f"{label}: Escape reaches the document handler ({mode})",
          esc >= 1 and (r.get("modality") == "dom") is expect_fallback,
          f"escapes={esc} modality={r.get('modality')}")

    # -- the wheel ----------------------------------------------------------
    # At pre-fallback HEAD the wheel dispatch never ACKs in a background tab and the
    # call raises Timeout after 10s — the baseline failure this check exists to record.
    try:
        r = tab.scroll(600)
    except Exception as e:                                        # noqa: BLE001
        r = {"error": f"{type(e).__name__}: {str(e)[:40]}"}
    check(f"{label}: scroll moves the page",
          r.get("y", 0) >= 400 and (r.get("modality") == "dom") is expect_fallback,
          f"y={r.get('y')} modality={r.get('modality')} {r.get('error', '')}")

    # -- the full combobox flow: open (mousedown), type (keyup), pick (click)
    tab.goto(f"{tab._base}/combobox.html")
    schema = form_schema(tab)
    combo = next(f for f in schema["fields"]
                 if f["kind"] == "combobox" and "Country" in str(f["label"]))
    out = select_option(tab, combo["ref"], "Schweiz")
    shown = tab.js("document.getElementById('i2').value")
    check(f"{label}: typeahead combobox end to end",
          out.ok and shown == "Schweiz",
          f"ok={out.ok} shown={shown!r} "
          + ("" if out.ok else str(out.detail)[:40]))

    # -- native state is evidence too: never double-toggle a checkbox -------
    tab.goto(f"{tab._base}/personio.html")
    schema = form_schema(tab)
    privacy = next(f for f in schema["fields"] if f.get("name") == "privacy")
    delta = tab.click_ref(privacy["ref"])
    checked = tab.js("document.querySelector('input[name=privacy]').checked")
    check(f"{label}: checkbox changes exactly once",
          checked is True
          and (delta["modality"] == "dom") is expect_fallback
          and delta["control_state_changed"] is True,
          f"checked={checked} modality={delta['modality']} "
          f"state_changed={delta['control_state_changed']}")


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="bh-bginput-"))
    site = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Site, directory=str(FIXTURES)))
    threading.Thread(target=site.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{site.server_port}"

    _browser.launch(scratch, window="1200,900")
    try:
        time.sleep(0.3)
        r = discover({"BH_PROFILE_DIRS": str(scratch)})
        conn = Connection(WebSocketTransport(r.ws_url),
                          journal=Journal(scratch / "s.jsonl", session="bg")).start()
        registry = SessionRegistry(conn)
        registry.discover()

        # A is created background; B after it, foreground — so A stays unselected the
        # whole run, exactly like nine of ten parallel() workers.
        a = conn.request("Target.createTarget",
                         {"url": "about:blank", "background": True})["targetId"]
        b = conn.request("Target.createTarget", {"url": "about:blank"})["targetId"]
        time.sleep(0.6)
        tab_a, tab_b = Tab(conn, registry, a), Tab(conn, registry, b)
        tab_a._base = base
        tab_b._base = base

        drive(tab_a, "background", expect_fallback=True)
        drive(tab_b, "selected  ", expect_fallback=False)

        conn.close()
    finally:
        _browser.kill(scratch)
        site.shutdown()
        shutil.rmtree(scratch, ignore_errors=True)

    failed = [n for n, ok_, _ in results if not ok_]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed"
          + (f" — FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
