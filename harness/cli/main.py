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

import sys

__all__ = ["main"]

USAGE = """bh — browser-harness v2

  bh <<'PY' ... PY      run a script against the browser (stdin)
  bh -                  same, explicit
  bh --doctor           classify why the browser can or cannot be reached
  bh daemon [name]      run the daemon in the foreground (usually auto-spawned)
  bh trace <file>       render a session journal as a span tree
  bh replay --diff A B  golden-file diff over two cassettes' request streams
  bh --version
"""


def main() -> int:
    args = sys.argv[1:]

    if args and args[0] == "--version":
        from importlib.metadata import PackageNotFoundError, version
        try:
            print(version("browser-harness-v2"))
        except PackageNotFoundError:
            print("0.0.1+src")
        return 0

    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return 0

    if args and args[0] == "--doctor":
        from harness.connect.doctor import diagnose, render
        outcome = diagnose(args[1] if len(args) > 1 else "default")
        for line in render(outcome):
            print(line)
        return 0 if outcome.ok else 1

    if args and args[0] == "daemon":
        from harness.connect.daemon import serve
        return serve(args[1] if len(args) > 1 else "default")

    if len(args) >= 2 and args[0] == "trace":
        from harness.core.journal import Journal
        from harness.core.trace import render as render_trace
        tail = int(args[args.index("--tail") + 1]) if "--tail" in args else None
        for line in render_trace(Journal(args[1]).entries(), tail=tail):
            print(line)
        return 0

    if len(args) >= 4 and args[0] == "replay" and args[1] == "--diff":
        import json

        from harness.core.cassette import diff
        report = diff(args[2], args[3])
        print(json.dumps(report, indent=2))
        return 0 if report["equal"] else 1

    if not args or args[0] == "-":
        if sys.stdin.isatty():
            print(USAGE, file=sys.stderr)
            return 2
        import os

        from harness.session import run_script
        return run_script(sys.stdin.read(), name=os.environ.get("BU_NAME", "default"))

    print(f"bh: unknown command {args[0]!r}\n\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
