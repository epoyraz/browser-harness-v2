"""bh — entry point. Stdin/heredoc only; the -c flag is a settled question (DESIGN.md §6)."""
import sys

__all__ = ["main"]


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--version":
        from importlib.metadata import PackageNotFoundError, version
        try:
            print(version("browser-harness-v2"))
        except PackageNotFoundError:
            print("0.0.1+src")
        return 0
    if args and args[0] == "--doctor":
        from harness.connect.doctor import diagnose, render
        outcome = diagnose()
        for line in render(outcome):
            print(line)
        return 0 if outcome.ok else 1
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
    print("bh — browser-harness v2 (scaffolding; see TODO.md)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
