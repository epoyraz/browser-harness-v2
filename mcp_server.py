"""MCP server exposing the browser-harness v2 helper surface over stdio.

    bh mcp                      # or: uv run python -m mcp_server

Modelled on v1's `mcp_server.py`, which settled two things this has to get right and
would otherwise have had to learn the hard way:

* **stdout belongs to the protocol.** Under stdio MCP, any stray `print` is a malformed
  JSON-RPC frame. Helpers here do print — progress, notes — so stdout is redirected to
  stderr for the duration of every call, where a client shows it as logs.
* **JS produces values `json.dumps` refuses.** `NaN` and `Infinity` come back from a page
  routinely, and `allow_nan=False` raises *before* `default` is consulted, so they are
  normalised rather than handled in an encoder hook.

Two things it does differently, because v2 has them and v1 does not:

* **Failures keep their class.** v1 returns `{"error": str(exc)}`; every v2 error carries
  an `Outcome`, so a tool result is the typed class, the evidence, whether it is
  retryable, and the recovery line. That is the difference between a client that can
  branch and one that can only match substrings.
* **Results pass the output ceiling.** A page dump that would flood the client spills to
  the content store and returns a digest, retrievable with `browser_fetch_content`.

`cdp` is deliberately not exposed. It is the raw protocol escape hatch, and a helper
surface that hands out arbitrary CDP over MCP is not a helper surface. `js` is exposed
because it is a documented helper with the dry-run and danger guards behind it.

Requires the optional dependency: `uv pip install "mcp>=2.0.0,<3"`.
"""
from __future__ import annotations

import functools
import json
import math
import sys
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from harness.core.outcome import HarnessError
from harness.session import Session

SERVER = MCPServer("browser-harness-v2")

#: One session for the life of the server. A tool call is short, an MCP client is not, and
#: reconnecting per call would spend a daemon handshake on every tool use.
_SESSION: Session | None = None


def _session() -> Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = Session("mcp")
    return _SESSION


def _ns() -> dict[str, Any]:
    return _session().namespace()


def _normalize(value: Any) -> Any:
    """Make a value JSON-safe before `json.dumps` sees it.

    `allow_nan=False` raises on NaN and Infinity *before* calling `default`, so a
    non-finite float from JS has to be replaced here rather than in the encoder hook.
    """
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_normalize(v) for v in value]
    return value


def _encode(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_json"):
        return value.to_json()
    return str(value)


def _dump(value: Any) -> str:
    return json.dumps(_normalize(value), ensure_ascii=False, allow_nan=False,
                      default=_encode)


@contextmanager
def _quiet_stdout():
    """Give stdout to the protocol and send everything else to stderr.

    One `print` inside a helper is a corrupt JSON-RPC frame and a dead session; a client
    surfaces stderr as logs, so nothing is lost by moving it there.
    """
    saved = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = saved


def _tool(fn):
    """Expose a function as an MCP tool returning JSON text, never raising.

    The failure shape is the point. A `HarnessError` already holds its outcome, so the
    client receives the class, the observed evidence, `retryable`, and the recovery line
    instead of a stringified exception it would have to parse.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            with _quiet_stdout():
                result = fn(*args, **kwargs)
            return _dump(_session()._bound_agent_value(fn.__name__, result))
        except HarnessError as error:
            return _dump(error.outcome.to_json())
        except Exception as error:                                  # noqa: BLE001
            # Not a browser failure: a bad argument, or a bug here. Say which rather than
            # dressing it up as a harness outcome class it does not belong to.
            return _dump({"ok": False, "class": "tool_error",
                          "detail": f"{type(error).__name__}: {error}"})

    return SERVER.tool(name=fn.__name__, description=fn.__doc__ or "")(wrapper)


# -- navigate and read --------------------------------------------------------------

@_tool
def browser_goto(url: str, wait_until: str = "load", timeout: float = 20.0):
    """Navigate the current tab to `url`. Returns the requested and landed URLs."""
    return _ns()["goto"](url, wait_until=wait_until, timeout=timeout)


@_tool
def browser_open_page(url: str, max_chars: int = 6000, max_links: int = 20):
    """Navigate and read in one call: text, links, metadata and challenge detection."""
    return _ns()["open_page"](url, max_chars=max_chars, max_links=max_links)


@_tool
def browser_read_page(max_chars: int = 6000, max_links: int = 20,
                      cursor: str | None = None):
    """Reread the current page as versioned semantic blocks.

    Prefer this to `browser_page_text`: it returns url, title, links and blocks together,
    and continuing with the returned `cursor` emits only what changed.
    """
    return _ns()["read_page"](max_chars=max_chars, max_links=max_links, cursor=cursor)


@_tool
def browser_page_text(max_chars: int = 12000, start: int = 0):
    """Rendered page text, bounded, paged by raw character offset."""
    return {"text": _ns()["page_text"](max_chars=max_chars, start=start)}


# -- find things --------------------------------------------------------------------

@_tool
def browser_find(text: str | None = None, pattern: str | None = None,
                 exclude: str | None = None, max_len: int | None = None,
                 role: str | None = None, tag: str | None = None, limit: int = 20):
    """Find elements by name, filtered in the page.

    `text` is a case-insensitive substring; `pattern` and `exclude` are regular
    expressions and `max_len` caps the name's length, which is what a real multilingual
    match needs. Rows carry refs the act tools take.
    """
    return _ns()["find"](text, pattern=pattern, exclude=exclude, max_len=max_len,
                         role=role, tag=tag, limit=limit)


@_tool
def browser_ax(name: str | None = None, role: str | None = None,
               pattern: str | None = None, exclude: str | None = None,
               limit: int = 20, refs: bool = True):
    """Elements as Chrome computes them: accessible name, role, and platform state.

    Use this when the name matters. `browser_find` reads an element's own markup and so
    misses `aria-labelledby`, `<label for>`, a wrapping `<label>`, `title` and an image's
    `alt`. Rows carry ordinary refs, at two round trips each; pass `refs=false` to read
    without acting.
    """
    return _ns()["ax"](name, role=role, pattern=pattern, exclude=exclude,
                       limit=limit, refs=refs)


@_tool
def browser_extract(selector: str, fields: dict | None = None, limit: int = 200):
    """Repeated records as rows, each with a ref.

    `fields` maps a name to a relative selector: `"h3"` for a descendant's text,
    `"a@href"` for an attribute, `"."` for the matched element. `matched` against
    `returned` says whether the ceiling bit.
    """
    return _ns()["extract"](selector, fields, limit=limit)


@_tool
def browser_snapshot():
    """Every interactive element with refs and viewport coordinates."""
    return _ns()["snapshot"]()


@_tool
def browser_form_schema():
    """The current form: labels, required flags, options and file inputs."""
    return _ns()["form_schema"]()


@_tool
def browser_form_values(ref: str | None = None):
    """What the form currently holds. Passwords read `[set]`, never the secret."""
    return _ns()["form_values"](ref)


# -- act ----------------------------------------------------------------------------

@_tool
def browser_click(ref: str, timeout: float = 10.0):
    """Click the element for `ref`. Returns the observed consequence of the click."""
    return _ns()["click_ref"](ref, timeout=timeout)


@_tool
def browser_set_value(ref: str, value: str, mode: str = "value"):
    """Write one field. `mode` is value | insert | type, escalating in trustworthiness."""
    return _ns()["set_value"](ref, value, mode=mode)


@_tool
def browser_select_option(ref: str, label: str):
    """Choose an option by its visible label, in a native select or an ARIA combobox."""
    return _ns()["select_option"](ref, label)


@_tool
def browser_fill_form(plan: list):
    """Write a whole form in one round trip.

    `plan` is a list of `{ref, value}` steps, usually derived from `browser_form_schema`.
    A refused write is escalated to the next mode automatically and reported per step.
    """
    return _ns()["fill_form"](plan)


@_tool
def browser_type(text: str, ref: str | None = None):
    """Type text as keystrokes, into `ref` or whatever holds focus."""
    return _ns()["type_chars"](text, ref=ref)


@_tool
def browser_press_key(key: str, modifiers: int = 0):
    """Press one named key, with optional CDP modifier bits."""
    return _ns()["press_key"](key, modifiers=modifiers)


@_tool
def browser_scroll(dy: int = 600, dx: int = 0):
    """Scroll the page, verified rather than assumed."""
    return _ns()["scroll"](dy=dy, dx=dx)


@_tool
def browser_upload_file(ref: str, paths: list):
    """Attach files to a file input without touching the OS picker."""
    return _ns()["upload_file"](ref, paths)


@_tool
def browser_wait_for(selector: str, state: str = "visible", timeout: float = 10.0):
    """Wait for a selector to reach a state, driven by mutations rather than polling."""
    return _ns()["wait_for"](selector, state=state, timeout=timeout)


# -- tabs, batch, evidence ----------------------------------------------------------

@_tool
def browser_new_tab(url: str = "about:blank"):
    """Open a tab this session owns and make it current."""
    return {"target_id": _ns()["new_tab"](url).target_id}


@_tool
def browser_use_tab(target_id: str):
    """Make an existing tab current."""
    return {"target_id": _ns()["use_tab"](target_id).target_id}


@_tool
def browser_close_tab(target_id: str | None = None):
    """Close a tab this session owns."""
    _ns()["close_tab"](target_id)
    return {"ok": True, "closed": target_id}


@_tool
def browser_targets():
    """Every page target the browser currently holds."""
    return _ns()["targets"]()


@_tool
def browser_fetch_all(urls: list, concurrency: int = 5, retries: int = 2):
    """Fetch many same-origin URLs from inside the page, with counted failures.

    Rides the page's own cookies. Reports attempted, succeeded and failed — a slot the
    pool never filled is a counted failure, never a silent gap.
    """
    return _ns()["fetch_all"](urls, concurrency=concurrency, retries=retries)


@_tool
def browser_fetch_content(digest: str):
    """Retrieve a value that exceeded the output ceiling, exactly, by its digest."""
    return _ns()["fetch_content"](digest)


@_tool
def browser_screenshot(path: str | None = None, quality: int = 70):
    """Capture the viewport to a PNG on disk. Returns the path."""
    return {"path": _ns()["capture_screenshot"](path, quality=quality)}


@_tool
def browser_see(marks: bool = True):
    """A screenshot plus the elements indexed on it, sharing one ref index.

    Escalate to this when structure and observed behaviour disagree.
    """
    return _ns()["see"](marks=marks)


@_tool
def browser_js(expression: str, timeout: float = 10.0):
    """Evaluate JavaScript in the page. The escape hatch, after the helpers above."""
    return {"value": _ns()["js"](expression, timeout=timeout)}


@_tool
def browser_doctor():
    """Classify whether the browser can be reached, and why not if it cannot."""
    from harness.connect.doctor import diagnose, to_json
    return to_json(diagnose("mcp"))


def main() -> int:
    try:
        SERVER.run()
    finally:
        if _SESSION is not None:
            _SESSION.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
