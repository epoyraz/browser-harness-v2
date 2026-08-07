"""Real-Chrome benchmark for the offline substantial-form corpus.

Run with `uv run python tests/bench/form_corpus.py`. The baseline deliberately mirrors
the multi-call dry-run sequence; the candidate uses `prepare_application()`. A candidate
has no right to survive if its schema differs or it does not reduce both calls and time.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "live"))

import _browser

from harness.connect.cdp import Connection, WebSocketTransport
from harness.connect.endpoint import discover
from harness.connect.session import SessionRegistry
from harness.core.journal import Journal
from harness.ops import forms
from harness.ops.page import SAFETY_JS, Tab
from harness.session import Session

CORPUS = ROOT / "tests/corpus/forms"


class _QuietSite(SimpleHTTPRequestHandler):
    def log_message(self, *_: Any) -> None:
        pass


class _LocalSession:
    prepare_application = Session.prepare_application

    def __init__(self, conn: Connection, registry: SessionRegistry, tab: Tab):
        self.conn = conn
        self.registry = registry
        self.journal = tab.journal
        self._tabs = {tab.target_id: tab}
        self._current = tab.target_id

    def tab(self, target_id: str | None = None) -> Tab:
        target_id = target_id or self._current
        if target_id not in self._tabs:
            self._tabs[target_id] = Tab(self.conn, self.registry, target_id,
                                        journal=self.journal)
        self._current = target_id
        return self._tabs[target_id]

    def use_tab(self, target_id: str) -> Tab:
        return self.tab(target_id)


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _stable(item) for key, item in value.items() if key != "ref"}
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


def _baseline(tab: Tab) -> dict[str, Any]:
    tab.js(SAFETY_JS)
    title = tab.js("document.title")
    language = tab.js("document.documentElement.lang || navigator.language || 'en'")
    url = tab.js("location.href")
    schema = forms.form_schema(tab)
    for frame in tab.frames():
        if frame.get("target_id"):
            pass
    file_inputs = tab.js("""[...document.querySelectorAll('input[type=file]')].map(el => ({
      name: el.name || el.id || 'file', accept: el.accept || '', multiple: !!el.multiple
    }))""")
    apply_link = tab.js("""(() => {
      const hit = [...document.querySelectorAll('a[href]')].find(a =>
        /^(apply|apply now|bewerben|jetzt bewerben|postuler|candidature|candida)/i
          .test((a.innerText || '').trim()));
      return hit ? hit.href : null;
    })()""")
    return {"schema": schema, "url": url, "title": title, "language": language,
            "file_inputs": file_inputs, "apply_link": apply_link}


def main() -> int:
    fixtures = sorted(path for path in CORPUS.iterdir() if path.is_dir())
    if len(fixtures) != 23:
        raise RuntimeError(f"expected 23 fixtures, found {len(fixtures)}")
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_QuietSite, directory=str(CORPUS)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    profile = Path(tempfile.mkdtemp(prefix="bh-corpus-bench-"))
    journal = Journal(Path(tempfile.mkdtemp()) / "corpus.jsonl", session="corpus")
    _browser.launch(profile, window="1200,900")
    conn: Connection | None = None
    rows = []
    counts = {"baseline": 0, "candidate": 0}
    external_requests: set[str] = set()
    phase = ""
    try:
        conn = Connection(WebSocketTransport(
            discover({"BH_PROFILE_DIRS": str(profile)}).ws_url), journal=journal).start()
        registry = SessionRegistry(conn, journal=journal)
        registry.discover()
        conn.subscribe(lambda message: external_requests.add(
            str((message.get("params") or {}).get("request", {}).get("url")))
            if message.get("method") == "Network.requestWillBeSent"
            and str((message.get("params") or {}).get("request", {}).get("url", "")).startswith(
                ("http://", "https://"))
            and not str((message.get("params") or {}).get("request", {}).get("url", "")).startswith(base)
            else None)
        target_id = conn.request("Target.createTarget", {"url": "about:blank"})["targetId"]
        tab = Tab(conn, registry, target_id, journal=journal)
        local = _LocalSession(conn, registry, tab)
        request = conn.request

        def counted_request(*args: Any, **kwargs: Any) -> dict[str, Any]:
            if phase:
                counts[phase] += 1
            return request(*args, **kwargs)

        conn.request = counted_request  # type: ignore[method-assign]
        for fixture in fixtures:
            url = f"{base}/{fixture.name}/index.html"
            tab.goto(url)
            phase = "baseline"
            started = time.perf_counter()
            baseline = _baseline(tab)
            baseline_ms = (time.perf_counter() - started) * 1000
            phase = ""

            tab.goto(url)
            phase = "candidate"
            started = time.perf_counter()
            candidate = local.prepare_application()
            candidate_ms = (time.perf_counter() - started) * 1000
            phase = ""
            same = _stable(baseline["schema"]) == _stable(candidate["schema"])
            rows.append({"fixture": fixture.name, "same_schema": same,
                         "baseline_ms": round(baseline_ms, 2),
                         "candidate_ms": round(candidate_ms, 2),
                         "fields": len(baseline["schema"].get("fields") or [])})
            print(f"{len(rows):02d}/{len(fixtures)} {fixture.name} "
                  f"baseline={baseline_ms:.1f}ms candidate={candidate_ms:.1f}ms "
                  f"same={same}", file=sys.stderr, flush=True)
    finally:
        if conn is not None:
            conn.close()
        server.shutdown()
        _browser.kill(profile)
        shutil.rmtree(profile, ignore_errors=True)

    baseline_ms = sum(row["baseline_ms"] for row in rows)
    candidate_ms = sum(row["candidate_ms"] for row in rows)
    mismatches = [row["fixture"] for row in rows if not row["same_schema"]]
    report = {
        "fixtures": len(rows), "schema_mismatches": mismatches,
        "external_network_requests": sorted(external_requests),
        "baseline": {"helper_calls": len(rows) * 8, "cdp_calls": counts["baseline"],
                     "ms": round(baseline_ms, 2)},
        "candidate": {"helper_calls": len(rows), "cdp_calls": counts["candidate"],
                      "ms": round(candidate_ms, 2)},
        "impact": {
            "helper_calls_saved": len(rows) * 7,
            "helper_reduction_pct": 87.5,
            "cdp_calls_saved": counts["baseline"] - counts["candidate"],
            "cdp_reduction_pct": round(
                (counts["baseline"] - counts["candidate"]) / counts["baseline"] * 100, 1),
            "ms_saved": round(baseline_ms - candidate_ms, 2),
            "speedup": round(baseline_ms / candidate_ms, 2),
        },
        "rows": rows,
    }
    print(json.dumps(report, indent=2))
    return 0 if not mismatches and not external_requests \
        and counts["candidate"] < counts["baseline"] \
        and candidate_ms < baseline_ms else 1


if __name__ == "__main__":
    raise SystemExit(main())
