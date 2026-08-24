"""Real-Chrome proof for bounded automatic public JSON endpoint replay.

Run manually with ``BH_HEADLESS=1 uv run python tests/live/endpoint_extraction_check.py``.
The fixture deliberately completes responses out of request order and also emits an
authenticated GET plus a mutating JSON POST. The automatic helper must make one anonymous
plan for the three public reads, preserve their request order, and omit both refusals.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _browser

from harness.connect.cdp import Connection, WebSocketTransport
from harness.connect.endpoint import discover
from harness.connect.session import SessionRegistry
from harness.core.journal import Journal
from harness.core.outcome import Class
from harness.ops.batch import fetch_observed_json
from harness.ops.page import Tab

HEADER_SECRET = "endpoint-header-secret-never-journal"
BODY_SECRET = "endpoint-body-secret-never-journal"
hits: Counter[str] = Counter()


class _Site(BaseHTTPRequestHandler):
    def _json(self, body: bytes = b"{}") -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:
        hits[self.path] += 1
        self._json()

    def do_POST(self) -> None:
        hits[self.path] += 1
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self._json(b'{"mutated":false}')

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        hits[self.path] += 1
        if path == "/":
            body = f"""<!doctype html><meta charset=utf-8><title>endpoints</title>
<script>
window.endpointDone = Promise.all([
  fetch('/api?page=1').then(response => response.json()),
  fetch('/api?page=2').then(response => response.json()),
  fetch('/api?page=3', {{method: 'HEAD'}}),
  fetch('/private', {{headers: {{'X-Api-Key': '{HEADER_SECRET}'}}}})
    .then(response => response.json()),
  fetch('/mutate', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{secret: '{BODY_SECRET}'}})}}).then(response => response.json())
]);
</script>""".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api":
            page = int(parse_qs(urlsplit(self.path).query)["page"][0])
            # Completion order differs from request order; output order must not.
            time.sleep({1: 0.12, 2: 0.01, 3: 0}.get(page, 0))
            self._json(json.dumps({"page": page}).encode())
            return
        if path == "/private":
            self._json(b'{"private":true}')
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args) -> None:
        pass


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="bh-endpoints-"))
    site = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{site.server_port}"
    journal_path = scratch / "session.jsonl"
    previous_trace = os.environ.get("BH_CDP_TRACE")
    os.environ["BH_CDP_TRACE"] = "1"

    _browser.launch(scratch, window="1000,760")
    conn = None
    try:
        resolution = discover({"BH_PROFILE_DIRS": str(scratch)})
        journal = Journal(journal_path, session="endpoint-live")
        conn = Connection(WebSocketTransport(resolution.ws_url), journal=journal).start()
        registry = SessionRegistry(conn, journal=journal)
        registry.discover()
        target_id = next(
            target["targetId"]
            for target in conn.request("Target.getTargets")["targetInfos"]
            if target["type"] == "page"
        )
        tab = Tab(conn, registry, target_id, journal=journal)
        tab.goto(base)
        tab.js("await window.endpointDone; true")

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            rows = tab._endpoint_snapshot()["observations"]
            public = [row for row in rows if "/api?" in str(row.get("url"))]
            if len(public) == 3 and all(
                    row.get("request_extra_complete")
                    and (not row.get("response_extra_expected")
                         or row.get("response_extra_seen"))
                    for row in public):
                break
            time.sleep(0.02)
        else:
            raise AssertionError("Chrome did not deliver complete public endpoint evidence")

        # Pay lazy isolated-world setup before measuring the plan itself.
        tab._world_js("true")
        out = fetch_observed_json(
            tab,
            max_urls=8,
            max_responses=8,
            max_total_bytes=10_000,
            concurrency=3,
            retries=1,
        )

        assert out.ok, out.to_json()
        assert [(row["method"], row["url"]) for row in out.value] == [
            ("GET", f"{base}/api?page=1"),
            ("GET", f"{base}/api?page=2"),
            ("HEAD", f"{base}/api?page=3"),
        ]
        assert out.observed["attempted"] == out.observed["succeeded"] == 3
        assert out.observed["failed"] == 0
        assert out.observed["response_count"] == 3
        assert out.observed["response_permits"] == 3
        assert out.observed["request_count"] == 3
        assert out.observed["total_bytes"] <= 10_000
        refusal_classes = {
            row["class"] for row in out.observed["selection"]["refusals"]
        }
        assert Class.SCOPE_REFUSED.value in refusal_classes
        assert Class.SIDE_EFFECT_REFUSED.value in refusal_classes
        assert hits["/private"] == 1 and hits["/mutate"] == 1
        assert all(hits[f"/api?page={page}"] == 2 for page in (1, 2, 3))

        fetch_spans = [
            entry for entry in journal.entries()
            if entry.get("kind") == "call" and entry.get("fn") == "fetch_observed_json"
        ]
        assert len(fetch_spans) == 1 and fetch_spans[0]["cdp"] == 1
        journal_text = journal_path.read_text(encoding="utf-8")
        assert HEADER_SECRET not in journal_text
        assert BODY_SECRET not in journal_text
        assert '"body"' not in journal_text and '"headers"' not in journal_text

        print("PASS: 3 ordered public JSON endpoints replayed in one anonymous CDP plan")
        print("PASS: authenticated GET and mutating POST refused without a second request")
        print("PASS: ExtraInfo evidence completed and header/body secrets absent from journal")
        return 0
    finally:
        if conn is not None:
            conn.close()
        _browser.kill(scratch)
        site.shutdown()
        shutil.rmtree(scratch, ignore_errors=True)
        if previous_trace is None:
            os.environ.pop("BH_CDP_TRACE", None)
        else:
            os.environ["BH_CDP_TRACE"] = previous_trace


if __name__ == "__main__":
    raise SystemExit(main())
