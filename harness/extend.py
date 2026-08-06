"""Agent-writable helpers (v1 parity, and the thesis v2 had dropped).

v1's `helpers.py` ends with `_load_agent_helpers()`: it imports
`$BH_AGENT_WORKSPACE/agent_helpers.py` and injects every public name into the namespace, so
an agent that needs a helper writes one mid-task and it is first-class on the next run.
That is the whole argument of the bitter-lesson post — the harness should be *editable by
the thing using it* — and v2 shipped without it. Adding a primitive to v2 meant editing the
library and re-running its tests, which is not something an agent can do inside a task.

The v2 version differs in one way that matters. v1's file is a plain module: it imports
`helpers` itself and calls the API through that import. Here the file is executed **with the
session namespace as its globals**, so an extension calls `goto()`, `snapshot()`,
`fill_form()` exactly as a `bh` script does — the same surface, no import ceremony:

    # ~/.browser-harness/helpers.py
    def apply_and_verify(url, plan):
        goto(url)
        out = fill_form(plan)
        return {"ok": out.ok, "url": js("location.href")}

Two files load, project last so it wins: `BH_HELPERS` (or `~/.browser-harness/helpers.py`),
then `./bh_helpers.py`. A broken extension is reported on stderr and skipped — it must not
cost you the browser — but it is never swallowed silently, because a helper that vanished
without explanation is worse than one that failed loudly.
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from typing import Any

#: Names an extension must not shadow. Rebinding these would let a helper file quietly
#: redefine what every later call in the same run means.
PROTECTED = frozenset({"session", "tab", "journal", "__builtins__", "__name__"})


def user_helpers_path() -> Path:
    if raw := os.environ.get("BH_HELPERS"):
        return Path(raw).expanduser()
    return Path.home() / ".browser-harness" / "helpers.py"


def candidates() -> list[Path]:
    """User file first, project file second — later wins, so a repo can override."""
    found = [user_helpers_path(), Path.cwd() / "bh_helpers.py"]
    return [p for p in found if p.is_file()]


def load_into(ns: dict[str, Any], *, paths: list[Path] | None = None,
              report: Any = None) -> list[dict[str, Any]]:
    """Execute each helper file with `ns` as its globals and merge back what it defined.

    Returns one record per file so the caller can surface what loaded. Executing *in* the
    namespace is what gives an extension the harness surface for free; the merge back is
    what makes its functions callable from the next script.
    """
    out: list[dict[str, Any]] = []
    for path in (candidates() if paths is None else paths):
        if not Path(path).is_file():
            continue          # a helper file you have not written yet is not an error
        record: dict[str, Any] = {"path": str(path), "added": []}
        before = set(ns)
        try:
            source = path.read_text(encoding="utf-8")
            exec(compile(source, str(path), "exec"), ns)   # noqa: S102 — that is the feature
        except Exception:                                  # noqa: BLE001 — reported, not raised
            record["error"] = traceback.format_exc(limit=3).strip().splitlines()[-1]
            # Loudly, on stderr: a helper that silently failed to load looks to the next
            # script exactly like a helper that was never written.
            print(f"bh: helper file {path} failed to load — {record['error']}",
                  file=report or sys.stderr)
        else:
            record["added"] = sorted(
                n for n in set(ns) - before
                if not n.startswith("_") and n not in PROTECTED and callable(ns.get(n)))
        out.append(record)
    return out


def scaffold(path: Path | None = None) -> Path:
    """Create a starter helper file. `bh helpers --init`, so the agent has somewhere to
    write rather than having to invent a location and hope it is loaded."""
    target = path or user_helpers_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text(
            '"""Your own browser-harness helpers.\n\n'
            "Executed with the `bh` script namespace as globals, so goto(), snapshot(),\n"
            "js(), fill_form(), see() … are all in scope. Every public function you define\n"
            "here is available in every `bh` script from the next run on.\n"
            '"""\n\n\n'
            "def page_summary():\n"
            '    """Example: url, title and the interactive element count."""\n'
            "    return {\n"
            '        "url": js("location.href"),\n'
            '        "title": js("document.title"),\n'
            '        "elements": len(snapshot()),\n'
            "    }\n",
            encoding="utf-8")
    return target
