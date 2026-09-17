from harness import version


def test_release_and_protocol_versions_are_explicit():
    assert version.VERSION == "0.1.0"
    assert isinstance(version.PROTOCOL_VERSION, int) and version.PROTOCOL_VERSION >= 1


def test_source_digest_changes_when_core_changes(tmp_path, monkeypatch):
    source = tmp_path / "version.py"
    source.write_text("first", encoding="utf-8")
    monkeypatch.setattr(version, "__file__", str(source))
    first = version.source_digest()
    source.write_text("second", encoding="utf-8")
    assert version.source_digest() != first
    source.write_text("first", encoding="utf-8")
    assert version.source_digest() == first
