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
    print("bh — browser-harness v2 (scaffolding; see TODO.md)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
