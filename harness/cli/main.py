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
from pathlib import Path

__all__ = ["main"]

USAGE = """bh — browser-harness v2

  bh <<'PY' ... PY      run a script against the browser (stdin)
  bh -                  same, explicit
  bh --doctor [--json]  classify why the browser can or cannot be reached
  bh mac-approve        answer Chrome's macOS "Allow remote debugging?" sheet
  bh daemon [name]      run the daemon in the foreground (usually auto-spawned)
  bh stats [path…]      what you actually use, and what actually fails
  bh bench <journal…>   steps taken and where the wall clock went (-v per step)
                        --from-transcript [FILE] price think from the real agent session
  bh recordings         list recordings (newest first)
  bh video [<rec>]      render a recording to mp4 (default: the newest)
  bh recording-extension  print the unpacked tabCapture extension path
  bh helpers --init     create a file for your own helpers
  bh skills which URL  explain offline skill resolution and trust
  bh skills search Q   search configured skill indexes
  bh skills show ID    verify and print a skill body
  bh skills sync       refresh configured Git sources
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
            from harness.version import VERSION
            print(f"{VERSION}+src")
        return 0

    if args and args[0] in ("-h", "--help"):
        print(USAGE)
        return 0

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

    if args and args[0] == "stats":
        import json as _json

        from harness.core.telemetry import render as render_stats
        from harness.core.telemetry import rollup
        rest = [a for a in args[1:] if not a.startswith("--")]
        r = rollup(rest or None)
        if "--json" in args:
            print(_json.dumps(r, indent=2))
        else:
            for line in render_stats(r):
                print(line)
        return 0

    if args and args[0] == "bench":
        import json as _json

        from harness.core.bench import render as render_bench
        from harness.core.bench import rollup
        rest = [a for a in args[1:] if not a.startswith("--")]
        think = None
        if "--think" in args:
            think = float(args[args.index("--think") + 1])
            rest = [a for a in rest if a != args[args.index("--think") + 1]]
        by_step = None
        if "--from-transcript" in args:
            # The one bucket the harness cannot see: `bh` is a subprocess the model
            # spawns, so the gap between runs happens entirely outside this process.
            # Claude Code's session transcript timestamps every tool_use and
            # tool_result, which is exactly that gap.
            from harness.core import transcript as tx
            i = args.index("--from-transcript") + 1
            given = args[i] if i < len(args) and not args[i].startswith("--") else None
            if given:
                rest = [a for a in rest if a != given]
            found = [Path(given)] if given else tx.find()
            if not found:
                print("no transcript found for this project", file=sys.stderr)
                return 1
            by_step = tx.attach(rollup(rest or ["."])["step_list"], tx.gaps(found[0]))
        r = rollup(rest or ["."], think_ms=think, think_by_step=by_step)
        if "--json" in args:
            print(_json.dumps({k: v for k, v in r.items() if k != "step_list"}, indent=2))
        else:
            for line in render_bench(r, verbose=("-v" in args or "--verbose" in args)):
                print(line)
        return 0

    if args and args[0] == "recordings":
        from harness.ops.record import recordings
        found = recordings()
        if not found:
            print("no recordings — use BH_RECORD=1, start_recording(), or start_screencast()",
                  file=sys.stderr)
            return 1
        for d in found:
            screencast = (d / "frames.jsonl").is_file()
            frames = len(list((d / "frames").glob("*.jpg"))) if screencast \
                else len(list(d.glob("*.jpg")))
            mode = "CDP screencast" if screencast else "action recording"
            print(f"{d}  ({frames} frame{'s' if frames != 1 else ''}, {mode})")
        return 0

    if args and args[0] == "recording-extension":
        path = Path(__file__).resolve().parents[1] / "assets" / "tab_recorder"
        print(path)
        return 0

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

    if args and args[0] == "video":
        from harness.ops.record import latest
        from harness.ops.video import export
        rest = [a for a in args[1:] if not a.startswith("--")]
        rec = rest[0] if rest else latest()
        if rec is None:
            print("no recording to export — `bh recordings` lists them", file=sys.stderr)
            return 1
        out = None
        if "--output" in args:
            out = args[args.index("--output") + 1]
        try:
            got = export(rec, out, overwrite="--overwrite" in args)
        except (RuntimeError, ValueError, FileExistsError, FileNotFoundError) as e:
            print(f"bh video: {e}", file=sys.stderr)
            return 1
        print(f"{got['path']}  {got['shots']} shots  {got['duration']:.1f}s "
              f"(real {got['real_duration']:.1f}s, {got['clamped']} clamped)  "
              f"{got['bytes'] // 1024} KB")
        return 0

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
        from harness.session import force_utf8_streams, run_script
        # Before the read, not after: the script itself arrives on stdin, so a non-ASCII
        # literal in it would fail to decode under Windows' ANSI default (upstream #359).
        force_utf8_streams()
        return run_script(sys.stdin.read(), name=os.environ.get("BU_NAME", "default"))

    print(f"bh: unknown command {args[0]!r}\n\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
