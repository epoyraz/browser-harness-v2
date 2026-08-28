"""CLI entry point. Heredoc/stdin is the settled interface (DESIGN.md §6, v1 #188→#343)."""
import subprocess
import sys


def _bh(*args, stdin="", env=None):
    return subprocess.run(
        [sys.executable, "-c",
         (f"import sys; sys.argv=['bh', *{list(args)!r}]; "
          "from harness.cli.main import main; raise SystemExit(main())")],
        # `bh` pins every stream to UTF-8 on every subcommand; read it as such rather than
        # in the console's code page, which turns the em dash in the usage line into "�".
        input=stdin, capture_output=True, text=True, encoding="utf-8", check=False, env=env)


def test_version_flag_prints_something():
    r = _bh("--version")
    assert r.returncode == 0 and r.stdout.strip()


def test_help_lists_the_heredoc_form_first():
    r = _bh("--help")
    assert r.returncode == 0
    assert "<<'PY'" in r.stdout                    # the interface, not an afterthought


def test_the_c_flag_is_rejected_not_implemented():
    """Merged in v1 (#188), documented (#215), then fully reverted (#343). Settled — and
    asserted by behaviour rather than by grepping the docstring that explains why."""
    r = _bh("-c", "print(1)")
    assert r.returncode == 2
    assert "unknown command" in r.stderr


def test_an_unknown_subcommand_exits_2_with_usage():
    r = _bh("frobnicate")
    assert r.returncode == 2 and "bh —" in r.stderr


def test_a_script_with_no_browser_reports_a_typed_outcome_not_a_traceback():
    """The contract has to survive to the surface the agent actually reads."""
    import os
    env = {**os.environ, "BH_RUNTIME_DIR": "/tmp/bh-cli-test",
           "BH_PROFILE_DIRS": "/tmp/bh-nonexistent-profile",
           "BU_CDP_URL": "", "BU_CDP_WS": "", "PYTHONPATH": os.getcwd()}
    r = _bh("-", stdin="goto('https://example.com')", env=env)
    assert r.returncode == 1
    assert "Traceback" not in r.stderr             # a class and its evidence, not a stack
    assert '"class"' in r.stderr
