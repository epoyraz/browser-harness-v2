"""CLI entry point. Heredoc/stdin is the settled interface (DESIGN.md §6, v1 #188→#343)."""
import subprocess, sys


def test_version_flag_prints_something():
    r = subprocess.run([sys.executable, "-c",
                        "import sys; sys.argv=['bh','--version']; "
                        "from harness.cli.main import main; raise SystemExit(main())"],
                       capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip()


def test_no_c_flag_exists():
    # -c was merged in v1 (#188), documented (#215), then fully reverted (#343). Settled.
    from harness.cli import main as m
    assert "-c" not in (m.main.__doc__ or "") and "-c" not in (m.__doc__ or "").replace("the -c flag", "")
