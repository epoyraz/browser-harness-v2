import json

import pytest

from harness.core import jsonl


def test_read_skips_malformed_and_non_object_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"ok": 1}\ntruncated\n[1, 2]\n{"ok": 2}\n')
    assert list(jsonl.read(path)) == [{"ok": 1}, {"ok": 2}]


def test_read_can_require_an_artifact(tmp_path):
    assert list(jsonl.read(tmp_path / "missing")) == []
    with pytest.raises(FileNotFoundError):
        list(jsonl.read(tmp_path / "missing", missing_ok=False))


def test_files_expands_directories_and_files(tmp_path):
    one, two = tmp_path / "one.jsonl", tmp_path / "nested" / "two.jsonl"
    two.parent.mkdir()
    one.write_text(json.dumps({"n": 1}))
    two.write_text(json.dumps({"n": 2}))
    assert set(jsonl.files([tmp_path])) == {one, two}
    assert jsonl.files([one]) == [one]
