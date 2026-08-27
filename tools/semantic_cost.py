"""What the semantic block cache actually saves, in the unit that matters.

`read_page()` promises that an unchanged second read returns block references instead of
replaying the same text. That is a token optimisation, and the case for deleting it rested
on a different optimisation in the same commit having measured badly — which is
suggestive, not evidence. This measures it directly: the same reads with the cache and
without, counting the bytes an agent would receive.

Bytes rather than tokens because the harness never sees a tokenizer; the conversion is
about four characters to one token and applies equally to both columns, so the ratio holds.

    uv run python tools/semantic_cost.py

Four scenarios, because the answer plausibly differs across them: a cold read, an
immediate repeat of an unchanged document, a repeat after one block changed, and a repeat
after a navigation. A cache that only wins on the second scenario is worth less than one
that wins on the third, since an agent that re-reads usually does so because it acted.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "live"))

#: A posting-shaped page: headings, paragraphs, a list, and a form. Long enough that a
#: bounded read is doing real work, and structured enough to have blocks to reference.
PARAGRAPHS = 40
_BODY = "\n".join(
    f'<p id="p{n}">Paragraph {n}. We are looking for an engineer who has shipped and '
    f'operated production systems, and who is comfortable owning a service end to end. '
    f'This paragraph exists to give the reader something of realistic length.</p>'
    for n in range(PARAGRAPHS))
_LIST = "".join(f"<li>Requirement {n}: several years of relevant experience.</li>"
                for n in range(12))
PAGE = f"""<!doctype html><meta charset=utf-8><title>Senior Engineer</title>
<h1>Senior Engineer</h1>
{_BODY}
<h2>Your profile</h2>
<ul>{_LIST}</ul>
<form><label for=a>First name</label><input id=a>
<label for=b>Email</label><input id=b type=email>
<button type=submit>Send application</button></form>
<script>
  window.mutateOne = () => {{
    const p = document.querySelector('#p7');
    p.textContent = 'REPLACED paragraph seven, with entirely different wording.';
  }};
</script>"""


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


def emitted(value: Any) -> int:
    """Bytes the agent would receive for this result."""
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def main() -> int:
    site = ThreadingHTTPServer(("127.0.0.1", 0), _Site)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{site.server_port}/"
    scratch = Path(tempfile.mkdtemp(prefix="bh-semcost-"))
    os.environ.setdefault("BH_HEADLESS", "1")
    os.environ["BH_PROFILE_DIRS"] = str(scratch)

    import _browser

    _browser.launch(scratch, window="1200,900")
    rows: list[tuple[str, int, int]] = []
    try:
        from harness.core.outcome import HarnessError
        from harness.session import Session

        session = Session("semcost")
        tab = session.tab()

        def read(cached: bool, cursor: str | None = None) -> dict[str, Any]:
            return tab._page_digest(max_chars=6_000, max_links=20, content_only=True,
                                    cursor=cursor, semantic=cached)

        # One cold read establishes the cursor; every later scenario continues from it.
        # Reading twice per scenario would let the first read absorb the change and the
        # second would then honestly report nothing new — which measures the rig, not the
        # cache.
        tab.goto(base)
        cold = read(True)
        cursor = cold.get("cursor")
        rows.append(("cold read", emitted(cold), emitted(read(False))))

        unchanged = read(True, cursor=cursor)
        rows.append(("repeat, unchanged", emitted(unchanged), emitted(read(False))))

        def continued(label: str) -> None:
            """Continue from the cold cursor, or record that it was refused."""
            try:
                value = read(True, cursor=cursor)
                print(f"  [{label}] changed={value.get('changed_count')} "
                      f"unchanged={value.get('unchanged_count')}")
            except HarnessError as error:
                # A cursor belongs to one document generation. When it is refused the
                # agent has to pay for a full read, so that is what the row must count.
                print(f"  [{label}] cursor refused: {error.outcome.cls.value}")
                value = read(True)
            rows.append((label, emitted(value), emitted(read(False))))

        tab.js("window.mutateOne()")
        continued("repeat, one block changed")

        tab.goto(base)
        continued("repeat, after navigation")
    finally:
        _browser.kill(scratch)
        site.shutdown()

    print(f"{'scenario':<28}{'with cache':>12}{'without':>10}{'delta':>10}")
    for label, cached, plain in rows:
        delta = (cached - plain) / plain if plain else 0.0
        print(f"{label:<28}{cached:>12,}{plain:>10,}{delta:>9.0%}")
    total_c = sum(r[1] for r in rows)
    total_p = sum(r[2] for r in rows)
    print(f"{'total':<28}{total_c:>12,}{total_p:>10,}"
          f"{(total_c - total_p) / total_p:>9.0%}")
    print(f"\n~{(total_p - total_c) // 4:,} tokens saved across {len(rows)} reads "
          f"(chars/4), on a {len(PAGE):,}-byte page")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
