"""Agent-writable helpers — the v1 capability v2 had dropped."""

from harness import extend
from harness.extend import PROTECTED, load_into, scaffold


def test_a_helper_is_executed_with_the_namespace_as_its_globals(tmp_path):
    """The whole ergonomic point: an extension calls the harness surface directly, with
    no import ceremony, exactly as a `bh` script does."""
    f = tmp_path / "h.py"
    f.write_text('def summary():\n    return js("document.title")\n')
    ns = {"js": lambda expr: f"evaluated:{expr}"}
    got = load_into(ns, paths=[f])
    assert got[0]["added"] == ["summary"]
    assert ns["summary"]() == "evaluated:document.title"


def test_later_files_win_so_a_repo_can_override(tmp_path):
    a, b = tmp_path / "a.py", tmp_path / "b.py"
    a.write_text("def who():\n    return 'user'\n")
    b.write_text("def who():\n    return 'project'\n")
    ns = {}
    load_into(ns, paths=[a, b])
    assert ns["who"]() == "project"


def test_a_broken_file_is_reported_and_skipped_not_swallowed(tmp_path, capsys):
    """Silently ignoring it makes a missing helper indistinguishable from one never
    written — but raising would cost you the browser over a typo."""
    bad, good = tmp_path / "bad.py", tmp_path / "good.py"
    bad.write_text("def x(:\n")
    good.write_text("def fine():\n    return 1\n")
    ns = {}
    got = load_into(ns, paths=[bad, good])
    assert "error" in got[0] and got[1]["added"] == ["fine"]
    assert ns["fine"]() == 1                       # the good file still loaded
    assert "failed to load" in capsys.readouterr().err


def test_the_session_handles_cannot_be_shadowed():
    assert {"session", "tab", "journal"} <= PROTECTED


def test_private_names_are_not_exported(tmp_path):
    f = tmp_path / "h.py"
    f.write_text("_secret = 1\ndef _hidden():\n    pass\ndef shown():\n    pass\n")
    ns = {}
    assert load_into(ns, paths=[f])[0]["added"] == ["shown"]


def test_scaffold_creates_a_runnable_starter(tmp_path):
    p = scaffold(tmp_path / "helpers.py")
    assert p.is_file() and "def page_summary()" in p.read_text()
    before = p.read_text()
    scaffold(p)
    assert p.read_text() == before                 # never clobbers your file


def test_a_missing_file_is_simply_not_loaded(tmp_path):
    assert load_into({}, paths=[tmp_path / "nope.py"]) == []


def test_an_unreadable_cwd_does_not_kill_the_session(monkeypatch):
    """macOS revokes per-app access to Desktop/Documents, and `Path.cwd()` then raises
    PermissionError. Because candidates() called it unguarded, *every* bh run died with a
    traceback three frames inside the loader — before the script ran, over an optional
    helper file that did not exist. Loading extensions must never be able to do that."""
    def boom():
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(extend.Path, "cwd", staticmethod(boom))
    monkeypatch.setenv("BH_HELPERS", "/nonexistent/helpers.py")

    assert extend.candidates() == []
    ns: dict = {}
    assert extend.load_into(ns) == []       # and the namespace still builds
