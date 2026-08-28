"""`bh` — entry point. Stdin/heredoc only; the -c flag is a settled question (DESIGN.md §6).

    bh <<'PY'
    goto("https://example.com")
    print(page_text()[:200])
    PY

Reading the script from stdin rather than `-c` was litigated in v1 (#188 merged, #215
documented, #343 fully reverted) and is not reopened here: a heredoc survives quoting,
newlines and embedded JS, which is most of what a browser script contains.
"""
from __future__ import annotations

import os
import sys

__all__ = ["main"]

USAGE = """bh — browser-harness v2

  bh <<'PY' ... PY      run a script against the browser (stdin)
  bh -                  same, explicit
  bh --doctor [--json]  classify why the browser can or cannot be reached
  bh mcp                serve the helper surface to MCP clients over stdio
  bh mac-approve        answer Chrome's macOS "Allow remote debugging?" sheet
  bh daemon [name]      run the daemon in the foreground (usually auto-spawned)
  bh helpers --init     create a file for your own helpers
  bh skills which URL  explain offline skill resolution and trust
  bh skills search Q   search configured skill indexes
  bh skills show ID    verify and print a skill body
  bh skills sync       refresh configured Git sources
  bh --version

Evidence commands (bh stats / bench / trace / recordings / video) come from the optional
evidence layer; run `bh stats --help` for its usage.
"""


def main() -> int:
    args = sys.argv[1:]
    # Every subcommand, not only the script runner: `bh bench` prints "→" and died under
    # cp1252 on Windows (2026-08-28) because the streams were only pinned to UTF-8 on the
    # way into `run_script`.
    from harness.session import force_utf8_streams
    force_utf8_streams()

    if args and args[0] == "--version":
        from importlib.metadata import PackageNotFoundError, version
        try:
            print(version("browser-harness-v2"))
        except PackageNotFoundError:
            from harness.version import VERSION
            print(f"{VERSION}+src")
        return 0

    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    # Commands belonging to the optional evidence layer. Core dispatches to it if it is
    # installed and never imports it by name otherwise: the direction of dependency is
    # what makes "core" measurable, and `bh stats` is not a browser primitive.
    try:
        from evidence.cli import handle as evidence_cli
    except ImportError:
        evidence_cli = None
    if evidence_cli is not None:
        handled = evidence_cli(args)
        if handled is not None:
            return handled

    if args and args[0] == "--doctor":
        import json as _json

        from harness.connect.doctor import diagnose, render, to_json
        rest = [a for a in args[1:] if not a.startswith("--")]
        outcome = diagnose(rest[0] if rest else "default")
        if "--json" in args:
            # `to_json`, not `outcome.to_json`: JSON output is piped, saved and pasted
            # into issues, so it reduces the endpoint to topology the way the journal
            # does. The human lines below keep the full URL — that terminal output is
            # ephemeral and the URL is the diagnosis.
            print(_json.dumps(to_json(outcome), indent=2))
        else:
            for line in render(outcome):
                print(line)
        return 0 if outcome.ok else 1

    if args and args[0] == "mcp":
        # The MCP surface needs the optional `mcp` package and lives outside `harness/`,
        # so core names it and never imports it eagerly — the same shape as the evidence
        # verbs above. `bh` keeps working when the dependency is not installed.
        try:
            from mcp_server import main as mcp_main
        except ImportError as error:
            print(f"bh mcp needs the optional dependency: uv pip install 'mcp>=2.0.0,<3'"
                  f"\n  ({error})", file=sys.stderr)
            return 2
        return mcp_main()

    if args and args[0] == "mac-approve":
        from harness.connect.macos import run_cli
        return run_cli(args[1:])

    if args and args[0] == "daemon":
        from harness.connect.daemon import serve
        # Foreground daemons used to ignore the same BH_JOURNAL contract every client
        # honors. That hid the only evidence capable of distinguishing a browser-websocket
        # failure from an overloaded client event queue. Share the append-only journal;
        # records contain protocol shape and counts, never page content.
        return serve(
            args[1] if len(args) > 1 else "default",
            journal_path=os.environ.get("BH_JOURNAL") or None,
        )

    if args and args[0] == "helpers":
        from harness.extend import candidates, scaffold
        if "--init" in args:
            print(f"created {scaffold()}")
            return 0
        found = candidates()
        if not found:
            print("no helper files — `bh helpers --init` creates one", file=sys.stderr)
            return 1
        for p in found:
            print(p)
        return 0

    if args and args[0] == "skills":
        import json as _json

        from harness.skills import Registry
        registry = Registry()
        command = args[1] if len(args) > 1 else ""
        value = args[2] if len(args) > 2 else ""
        if command == "which" and value:
            found = registry.match(value)
            print(_json.dumps([ref.to_json() for ref in found], indent=2))
            return 0 if found else 1
        if command == "search" and value:
            print(_json.dumps([ref.to_json() for ref in registry.search(value)], indent=2))
            return 0
        if command == "show" and value:
            found = registry.search(value)
            exact = next((ref for ref in found if ref.id == value), None)
            if exact is None:
                print(f"bh skills: no skill {value!r}", file=sys.stderr)
                return 1
            print(registry.load(exact).for_model())
            return 0
        if command == "sync":
            print(_json.dumps(registry.sync(value or None), indent=2))
            return 0
        print("usage: bh skills which URL | search Q | show ID | sync [SOURCE]",
              file=sys.stderr)
        return 2

    if not args or args[0] == "-":
        if sys.stdin.isatty():
            print(USAGE, file=sys.stderr)
            return 2
        from harness.session import run_script
        # Before the read, not after: the script itself arrives on stdin, so a non-ASCII
        # literal in it would fail to decode under Windows' ANSI default (upstream #359).
        force_utf8_streams()
        return run_script(sys.stdin.read(), name=os.environ.get("BU_NAME", "default"))

    print(f"bh: unknown command {args[0]!r}\n\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
