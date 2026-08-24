import json

import pytest

from harness.core.content import ContentStore, OutputCapture


def test_values_round_trip_losslessly_by_typed_digest(tmp_path):
    store = ContentStore(tmp_path)
    values = ["hello \u6c42\u4eba", b"\x00\xff", {"z": [1, True], "a": "x"}]
    refs = [store.put(value) for value in values]

    assert [store.get(ref.digest) for ref in refs] == values
    assert len({ref.digest for ref in refs}) == len(values)


def test_identical_values_dedupe_and_large_values_become_reversible_markers(tmp_path):
    store = ContentStore(tmp_path)
    value = {"body": "x" * 10_000}
    first = store.elide(value, limit=100, surface="js")
    second = store.elide(value, limit=100, surface="js")

    assert first["_sha256"] == second["_sha256"]
    assert first["surface"] == "js" and "fetch_content" in first["retrieval"]
    assert store.get(first["_sha256"]) == value
    assert len([path for path in tmp_path.rglob("*") if path.is_file()]) == 1


def test_bad_or_tampered_digest_fails_closed(tmp_path):
    store = ContentStore(tmp_path)
    with pytest.raises(ValueError):
        store.get("../escape")
    ref = store.put("secret")
    path = tmp_path / ref.digest[:2] / ref.digest[2:]
    path.write_text("text\nchanged", encoding="utf-8")
    with pytest.raises(ValueError, match="mismatch"):
        store.get(ref.digest)


def test_storage_failure_never_returns_a_marker_that_cannot_be_retrieved(tmp_path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("occupied", encoding="utf-8")
    store = ContentStore(blocked)

    with pytest.raises(OSError):
        store.elide("x" * 1_000, limit=10, surface="js")


class _Target:
    encoding = "utf-8"

    def __init__(self):
        self.value = ""

    def write(self, value):
        self.value += value

    def flush(self):
        pass


def test_stdout_ceiling_applies_to_the_complete_invocation_and_is_reversible(tmp_path):
    store, target = ContentStore(tmp_path), _Target()
    capture = OutputCapture(target, store, limit=40)
    for _ in range(20):
        capture.write("abcdefghij")

    stats = capture.emit()
    marker = json.loads(target.value)

    assert stats["output_bytes"] == 200 and stats["output_truncated"] is True
    assert len(target.value.encode()) < 800
    assert store.get(marker["_sha256"]) == "abcdefghij" * 20


def test_small_stdout_is_byte_for_byte_unchanged(tmp_path):
    target = _Target()
    capture = OutputCapture(target, ContentStore(tmp_path), limit=100)
    capture.write("one")
    capture.write("\ntwo\n")
    stats = capture.emit()

    assert target.value == "one\ntwo\n"
    assert stats == {"output_bytes": 8, "output_emitted_bytes": 8,
                     "output_truncated": False}
