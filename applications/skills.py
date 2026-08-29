"""Machine-actionable apply hints carried by skills.

A skill body is markdown for a model. The rules planner does not read prose (measured
2026-08-09: prose skills changed nothing), so a skill that wants to change what the
workflow *does* carries a fenced ``json`` block with one ``apply`` object::

    ```json
    {"apply": {"mode": "account", "ats": "Workday", "renders_hidden": false,
               "routes": [{"from": "https://x/jobs/1", "to": "https://x/jobs/1/apply"}]}}
    ```

Fields, all optional: ``mode`` (form | account | email — feeds the dispatch filter),
``ats`` (vendor name, feeds the same filter), ``renders_hidden`` (false = the page paints
only in a visible tab), ``routes`` (exact ``from`` URL or ``host`` glob → application view,
tried before the posting), ``notes``. Higher source priority wins per field; the harness
registry (`harness.skills.Registry`) does matching, digests and trust; this module only
interprets the block. Zero CDP.
"""
from __future__ import annotations

import fnmatch
import json
import re
from typing import Any
from urllib.parse import urlsplit

from harness.skills import Registry

_BLOCK = re.compile(r"```json\s*\n(\{.*?\})\s*\n```", re.DOTALL)


def parse_apply_block(body: str) -> dict[str, Any]:
    """The ``apply`` object of the first well-formed fenced json block, else ``{}``."""
    for match in _BLOCK.finditer(body or ""):
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("apply"), dict):
            return dict(data["apply"])
    return {}


def _routes_for(routes: Any, url: str) -> list[str]:
    host = (urlsplit(url).hostname or "").lower()
    out: list[str] = []
    for row in routes or []:
        if not isinstance(row, dict) or not row.get("to"):
            continue
        src = str(row.get("from") or "")
        if src and src.startswith("http"):
            if src.rstrip("/") != url.rstrip("/"):
                continue
        elif src and not fnmatch.fnmatch(host, src.lower()):
            continue
        to = str(row["to"])
        if to not in out and to.rstrip("/") != url.rstrip("/"):
            out.append(to)
    return out


def apply_hints(session: Any, url: str, *, registry: Registry | None = None) -> dict[str, Any]:
    """Merged hints for one URL: ``{mode, ats, renders_hidden, routes, ids, bytes}``."""
    reg = registry
    if reg is None:
        if getattr(session, "_skill_registry", None) is None:
            session._skill_registry = Registry()
        reg = session._skill_registry
    hints: dict[str, Any] = {"mode": None, "ats": None, "renders_hidden": None,
                             "routes": [], "ids": [], "bytes": 0,
                             # the action cache: control labels/selectors that led to a form
                             "actions": {"labels": [], "selectors": []}}
    # refs come back sorted by (priority, id, version) descending: first writer wins
    for ref in reg.match(url):
        try:
            body = reg.load(ref).content
        except Exception as error:  # noqa: BLE001 — a bad body is not a reason to stop the item
            journal = getattr(session, "journal", None)
            if journal is not None:
                journal.write("note", event="skill_body_unreadable", skill=ref.id,
                              source=ref.source, error=f"{type(error).__name__}: {str(error)[:120]}")
            continue
        block = parse_apply_block(body)
        if not block:
            continue
        hints["ids"].append(ref.id)
        hints["bytes"] += len(body.encode("utf-8"))
        for key in ("mode", "ats", "renders_hidden"):
            if hints[key] is None and block.get(key) is not None:
                hints[key] = block[key]
        for route in _routes_for(block.get("routes"), url):
            if route not in hints["routes"]:
                hints["routes"].append(route)
        for action in block.get("actions") or []:
            if not isinstance(action, dict):
                continue
            label = str(action.get("label") or "").strip()
            selector = str(action.get("selector") or "").strip()
            if label and label not in hints["actions"]["labels"]:
                hints["actions"]["labels"].append(label)
            if selector and selector not in hints["actions"]["selectors"]:
                hints["actions"]["selectors"].append(selector)
    return hints


def with_hints(job: dict[str, Any], hints: dict[str, Any]) -> dict[str, Any]:
    """The posting with skill-supplied ``apply.mode``/``apply.ats`` where joblens had none."""
    apply = dict(job.get("apply") or {})
    mode = str(apply.get("mode") or "").strip().lower()
    if mode in ("", "unknown") and hints.get("mode"):
        apply["mode"] = str(hints["mode"])
        apply["mode_source"] = "skill"
    if not apply.get("ats") and hints.get("ats"):
        apply["ats"] = str(hints["ats"])
        apply["ats_source"] = "skill"
    return {**job, "apply": apply}
