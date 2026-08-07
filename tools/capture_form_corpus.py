# ruff: noqa: BLE001, S110, S112
"""Capture substantial application forms as deterministic, offline HTML fixtures.

The source list is the latest attempt for every job whose dry run filled at least four
fields. Capture uses one isolated Chrome instance with at most five worker tabs. The
saved pages contain no application values, executable site scripts, or live form action.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "live"))

import _browser

from harness.connect.cdp import Connection, WebSocketTransport
from harness.connect.endpoint import discover
from harness.connect.session import SessionRegistry
from harness.ops.forms import form_schema
from harness.ops.page import Tab

DEFAULT_ATTEMPTS = ROOT / "outputs/application-form-dry-run-final/logs/attempts.jsonl"
DEFAULT_OUTPUT = ROOT / "tests/corpus/forms"
MAX_TABS = 5

CAPTURE_JS = r"""(() => {
  const documents = [document];
  for (let index = 0; index < documents.length; index++) {
    for (const frame of documents[index].querySelectorAll('iframe')) {
      try { if (frame.contentDocument) documents.push(frame.contentDocument); } catch (_) {}
    }
  }
  const doc = documents.sort((a, b) =>
    b.querySelectorAll('input:not([type=hidden]),select,textarea,[role=combobox],[contenteditable=true]').length -
    a.querySelectorAll('input:not([type=hidden]),select,textarea,[role=combobox],[contenteditable=true]').length
  )[0];
  const controls = [...doc.querySelectorAll(
    'input:not([type=hidden]),select,textarea,[role=combobox],[contenteditable=true]')];
  const submits = [...doc.querySelectorAll(
    'button[type=submit],input[type=submit],button:not([type])')];
  if (!controls.length) return null;

  let root = controls[0].closest('form');
  if (!root || !controls.every(el => root.contains(el))) {
    root = controls[0];
    while (root.parentElement &&
           !controls.every(el => root.contains(el)) && root !== document.body)
      root = root.parentElement;
  }
  if (submits.length && !submits.some(el => root.contains(el))) {
    while (root.parentElement && !submits.some(el => root.contains(el)) &&
           root !== document.body) root = root.parentElement;
  }

  const clone = root.cloneNode(true);
  const originals = [root, ...root.querySelectorAll('*')];
  const copies = [clone, ...clone.querySelectorAll('*')];
  const props = [
    'display', 'visibility', 'position', 'box-sizing', 'width', 'height',
    'min-width', 'min-height', 'max-width', 'max-height', 'margin', 'padding',
    'border', 'font', 'line-height', 'white-space', 'overflow', 'clip',
    'clip-path', 'opacity', 'flex', 'flex-direction', 'flex-wrap', 'gap',
    'grid-template-columns', 'grid-template-rows', 'align-items', 'justify-content'
  ];
  for (let i = 0; i < copies.length; i++) {
    const source = originals[i], target = copies[i];
    if (!source || !target || target.nodeType !== Node.ELEMENT_NODE) continue;
    const style = doc.defaultView.getComputedStyle(source);
    target.removeAttribute('style');
    for (const prop of props) {
      const value = style.getPropertyValue(prop);
      if (value) target.style.setProperty(prop, value);
    }
    target.removeAttribute('integrity');
    target.removeAttribute('nonce');
    for (const attr of [...target.attributes]) {
      if (/^on/i.test(attr.name)) target.removeAttribute(attr.name);
    }
    if (source.tagName === 'INPUT') {
      if (!['submit', 'button', 'reset'].includes((source.type || '').toLowerCase()))
        target.removeAttribute('value');
      target.removeAttribute('checked');
    } else if (source.tagName === 'TEXTAREA') {
      target.textContent = '';
    } else if (source.tagName === 'SELECT') {
      for (const option of target.options) option.removeAttribute('selected');
      if (target.options.length) target.options[0].setAttribute('selected', '');
    }
  }
  for (const node of clone.querySelectorAll(
    'script,noscript,style,link,iframe,video,audio,source,img,picture,svg'))
    node.remove();
  for (const form of [clone, ...clone.querySelectorAll('form')]) {
    if (form.tagName === 'FORM') {
      form.setAttribute('action', '#blocked');
      form.setAttribute('method', 'get');
    }
  }
  for (const link of clone.querySelectorAll('a[href]')) link.setAttribute('href', '#');
  return {
    html: clone.outerHTML,
    language: doc.documentElement.lang || document.documentElement.lang || 'en',
    title: doc.title || document.title,
    source: doc.location.href,
    control_count: controls.length
  };
})()"""


def _latest_substantial_attempts(path: Path) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        attempt = json.loads(line)
        key = str(attempt["attempt"])
        if key not in latest or float(attempt.get("finished", 0)) >= float(
                latest[key].get("finished", 0)):
            latest[key] = attempt
    selected = [item for item in latest.values() if int(item.get("filled_count", 0)) >= 4]
    return sorted(selected, key=lambda item: int(item.get("number", 0)))


def _stable_schema(schema: dict[str, Any]) -> dict[str, Any]:
    fields = []
    for field in schema.get("fields") or []:
        fields.append({key: value for key, value in field.items() if key != "ref"})
    return {"verdict": schema.get("verdict") or {}, "fields": fields,
            "files": schema.get("files") or []}


def _document(snapshot: dict[str, Any], attempt: dict[str, Any]) -> str:
    title = str(snapshot.get("title") or attempt.get("title") or "Application form")
    language = str(snapshot.get("language") or "en")
    source = str(snapshot.get("source") or attempt.get("landed_url") or attempt["url"])
    return f"""<!doctype html>
<html lang={json.dumps(language)}>
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'">
<meta name="bh-source-url" content={json.dumps(source)}>
<title>{title.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')}</title>
<style>html,body{{margin:0;padding:16px;min-height:100%}} body{{font-family:Arial,sans-serif}}</style>
<script>
document.addEventListener('submit', event => {{ event.preventDefault(); event.stopImmediatePropagation(); }}, true);
addEventListener('DOMContentLoaded', () => {{
  HTMLFormElement.prototype.submit = function() {{ return false; }};
  HTMLFormElement.prototype.requestSubmit = function() {{ return false; }};
}});
</script>
</head>
<body data-bh-fixture={json.dumps(str(attempt['attempt']))}>
{snapshot['html']}
</body>
</html>
"""


def _capture_one(conn: Connection, registry: SessionRegistry, output: Path,
                 attempt: dict[str, Any]) -> dict[str, Any]:
    target_id = conn.request("Target.createTarget", {"url": "about:blank"})["targetId"]
    tab = Tab(conn, registry, target_id)
    selected_tab = tab
    context = "main"
    try:
        navigation = tab.goto(attempt["url"], timeout=35)
        schema = form_schema(tab)
        if len(schema.get("fields") or []) < 4:
            try:
                tab.wait_for("form input:not([type=hidden]),form textarea,form select",
                             state="visible", timeout=12, settle=0.5)
            except Exception:  # a later schema still carries useful evidence
                pass
            schema = form_schema(tab)
        if len(schema.get("fields") or []) < 4:
            navigation = tab.goto(attempt["url"], timeout=35)
            try:
                tab.wait_for("form input:not([type=hidden]),form textarea,form select",
                             state="visible", timeout=12, settle=0.5)
            except Exception:
                pass
            schema = form_schema(tab)
        if len(schema.get("fields") or []) < 4:
            candidates: list[tuple[Tab, dict[str, Any], str]] = []
            for frame in tab.frames():
                frame_id = frame.get("target_id")
                if not frame_id:
                    continue
                try:
                    frame_tab = Tab(conn, registry, frame_id)
                    frame_schema = form_schema(frame_tab)
                    candidates.append((frame_tab, frame_schema, str(frame.get("kind") or "iframe")))
                except Exception:
                    continue
            if candidates:
                selected_tab, schema, context = max(
                    candidates,
                    key=lambda item: (bool((item[1].get("verdict") or {}).get("is_form")),
                                      len(item[1].get("fields") or [])),
                )
        if len(schema.get("fields") or []) < 4:
            raise RuntimeError(f"live form exposed only {len(schema.get('fields') or [])} fields")
        snapshot = selected_tab.js(CAPTURE_JS, timeout=30)
        if (not snapshot or not snapshot.get("html")) and selected_tab is not tab:
            snapshot = tab.js(CAPTURE_JS, timeout=30)
        if not snapshot or not snapshot.get("html"):
            raise RuntimeError("rendered form snapshot was empty")
        destination = output / str(attempt["attempt"])
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "index.html").write_text(_document(snapshot, attempt), encoding="utf-8")
        expected = {
            "attempt": attempt["attempt"],
            "number": attempt.get("number"),
            "company": attempt.get("company"),
            "title": attempt.get("title"),
            "source_url": attempt.get("url"),
            "landed_url": navigation.get("landed") or snapshot.get("source"),
            "original_context": context,
            "dry_run_filled_count": attempt.get("filled_count"),
            "schema": _stable_schema(schema),
        }
        (destination / "expected.json").write_text(
            json.dumps(expected, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return {"attempt": attempt["attempt"], "ok": True,
                "fields": len(schema.get("fields") or []),
                "captured_controls": snapshot.get("control_count"), "context": context}
    except Exception as error:
        return {"attempt": attempt["attempt"], "ok": False,
                "error": f"{type(error).__name__}: {error}"[:300]}
    finally:
        try:
            tab.close()
            registry.forget(target_id)
            conn.request("Target.closeTarget", {"targetId": target_id})
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempts", type=Path, default=DEFAULT_ATTEMPTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--workers", type=int, default=MAX_TABS)
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    attempts = _latest_substantial_attempts(args.attempts)
    if args.only:
        wanted = set(args.only)
        attempts = [attempt for attempt in attempts if attempt["attempt"] in wanted]
        missing = wanted - {attempt["attempt"] for attempt in attempts}
        if missing:
            raise RuntimeError(f"unknown attempts: {sorted(missing)}")
    elif len(attempts) != 23:
        raise RuntimeError(f"expected 23 substantial attempts, found {len(attempts)}")
    workers = max(1, min(args.workers, MAX_TABS, len(attempts)))
    if args.output.exists() and not args.keep:
        shutil.rmtree(args.output)
    args.output.mkdir(parents=True, exist_ok=True)

    profile = Path(tempfile.mkdtemp(prefix="bh-corpus-"))
    results: list[dict[str, Any]] = []
    started = time.perf_counter()
    _browser.launch(profile, window="1200,900")
    conn: Connection | None = None
    try:
        endpoint = discover({"BH_PROFILE_DIRS": str(profile)})
        conn = Connection(WebSocketTransport(endpoint.ws_url)).start()
        registry = SessionRegistry(conn)
        registry.discover()
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="corpus") as pool:
            futures = [pool.submit(_capture_one, conn, registry, args.output, attempt)
                       for attempt in attempts]
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                state = "PASS" if result["ok"] else "FAIL"
                print(f"{state} {result['attempt']}: "
                      f"{result.get('fields', result.get('error', ''))}", flush=True)
    finally:
        if conn is not None:
            conn.close()
        _browser.kill(profile)
        shutil.rmtree(profile, ignore_errors=True)

    results.sort(key=lambda result: result["attempt"])
    manifest_path = args.output / "manifest.json"
    if args.keep and manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        merged = {result["attempt"]: result for result in previous.get("results", [])}
        merged.update({result["attempt"]: result for result in results})
        results = sorted(merged.values(), key=lambda result: result["attempt"])
    manifest = {"generated_at": int(time.time()), "source_attempts": str(args.attempts),
                "selection": "latest unique attempts with filled_count >= 4",
                "workers": workers, "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "results": results}
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    failed = [result for result in results if not result["ok"]]
    print(json.dumps({"selected": len(attempts), "captured": len(results) - len(failed),
                      "failed": len(failed), "duration_ms": manifest["duration_ms"]}))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
