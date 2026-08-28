# Serving v2 to MCP clients

    uv pip install "mcp>=2.0.0,<3"
    bh mcp                          # or: uv run python -m mcp_server

Twenty-nine tools over stdio: navigate and read, `find` / `ax` / `extract`, the act
helpers, tabs, `fetch_all`, screenshots, and `--doctor`.

## Registering it with Claude Code

```bash
claude mcp add browser-harness -- \
  uv run --directory /path/to/browser-harness/v2 python -m mcp_server
```

Or by hand, in `~/.claude.json` (user scope) or a project `.mcp.json`:

```json
{
  "mcpServers": {
    "browser-harness": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/browser-harness/v2",
               "python", "-m", "mcp_server"]
    }
  }
}
```

Tools then appear to the client as `mcp__browser-harness__browser_goto` and so on. The
same command works for any stdio MCP client — Cursor, Windsurf, Continue, the Claude
desktop app — since none of it is Claude-Code-specific.

Verified: the server answers `initialize` with protocol `2025-06-18`, advertises tools,
prompts and resources, and drives a real navigation. `tests/live/mcp_check.py` runs the
whole exchange over the wire.

## Why a server rather than the CLI

`bh` is reachable only by an agent that can run a shell command. Most agent hosts speak
MCP and some cannot spawn processes at all, so without this v2 is unavailable to them.
That is a distribution gap, not a capability one.

## What it does differently from a thin wrapper

**Failures keep their class.** A tool result for a failure is the typed outcome — class,
observed evidence, `retryable`, and a recovery line:

```json
{"ok": false, "class": "element_gone", "detail": "no element registered for ref 'e999'",
 "retryable": false, "recovery": "the ref no longer resolves — re-read with snapshot()"}
```

A client can branch on that. `{"error": "<str(exception)>"}` can only be pattern-matched.

**Results pass the output ceiling.** A three-megabyte string from `browser_js` returns as
746 bytes and a digest, retrievable with `browser_fetch_content`. Without it, one
accidental page dump is the client's whole context.

**stdout belongs to the protocol.** Helpers print — progress, notes — and under stdio MCP
a stray `print` is a malformed frame and a dead session. Every call runs with stdout
redirected to stderr, where the client shows it as logs.

**Non-finite floats are normalised.** `NaN` and `Infinity` come back from a page
routinely, and `json.dumps(allow_nan=False)` raises *before* the encoder hook, so they are
replaced rather than handled there.

## What is deliberately not exposed

`cdp`. It is the raw-protocol escape hatch, and a helper surface that hands out arbitrary
CDP over MCP is not a helper surface. `js` is exposed, because it is a documented helper
with the dry-run and danger guards behind it.

## One session, one browser

The server holds a single `Session` for its lifetime and closes it on exit. Tool calls are
short and MCP clients are not; reconnecting per call would spend a daemon handshake on
every tool use.
