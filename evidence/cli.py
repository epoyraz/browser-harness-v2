"""`bh` verbs that belong to the evidence layer.

`bh stats`, `bh bench`, `bh trace`, `bh recordings` and `bh video` read journals and
frames. None of them drives a browser, so none of them is core — but they are still `bh`
subcommands, because that is where a user looks for them.

`handle()` returns an exit code for a verb it owns and `None` for anything else, so the
core CLI dispatches here without importing this package by name and works unchanged when
it is absent.
"""
from __future__ import annotations

import sys
from pathlib import Path

USAGE = """  bh stats [path…]      what you actually use, and what actually fails
  bh bench <journal…>   steps taken and where the wall clock went (-v per step)
                        --from-transcript [FILE] price think from the real agent session
  bh recordings         list recordings (newest first)
  bh video [<rec>]      render a recording to mp4 (default: the newest)
  bh recording-extension  print the unpacked tabCapture extension path
  bh trace <file>       render a session journal as a span tree"""


def handle(args: list[str]) -> int | None:
    """Run an evidence verb, or return None if this is not one of ours."""
    if args and args[0] == "stats":
        import json as _json

        from evidence.telemetry import render as render_stats
        from evidence.telemetry import rollup
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

        from evidence.bench import render as render_bench
        from evidence.bench import rollup
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
            from evidence import transcript as tx
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
        from evidence.record import recordings
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
    if args and args[0] == "video":
        from evidence.record import latest
        from evidence.video import export
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
        from evidence.trace import render as render_trace
        from harness.core.journal import Journal
        tail = int(args[args.index("--tail") + 1]) if "--tail" in args else None
        for line in render_trace(Journal(args[1]).entries(), tail=tail):
            print(line)
        return 0
    return None
