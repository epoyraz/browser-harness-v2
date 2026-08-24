import pytest

from harness.core.content import ContentStore
from harness.core.journal import Journal
from harness.core.outcome import Class, DocumentVersionStale
from harness.ops.semantic import SemanticPageCache


def _raw(*texts, document_id=1):
    return {
        "url": "https://example.test/a", "title": "A", "document_id": document_id,
        "ready_state": "complete", "language": "en", "links": [],
        "challenge": {"detected": False},
        "blocks": [{"kind": "paragraph", "key": f"main>p:{i}", "text": text}
                   for i, text in enumerate(texts)],
    }


def test_unchanged_second_read_returns_refs_without_replaying_text(tmp_path):
    cache = SemanticPageCache(ContentStore(tmp_path), Journal(None), "a")
    first = cache.render(_raw("one", "two"), max_chars=100, max_links=10)
    second = cache.render(_raw("one", "two"), max_chars=100, max_links=10)

    assert [block["text"] for block in first["blocks"]] == ["one", "two"]
    assert second["blocks"] == [] and second["text"] == ""
    assert second["unchanged_refs"] == [block["ref"] for block in first["blocks"]]
    assert second["repeated_output_bytes"] == 6


def test_only_meaningfully_changed_blocks_are_emitted(tmp_path):
    cache = SemanticPageCache(ContentStore(tmp_path), Journal(None), "a")
    first = cache.render(_raw("one", "two"), max_chars=100, max_links=10)
    changed = cache.render(_raw("one", "TWO"), max_chars=100, max_links=10)

    assert [block["text"] for block in changed["blocks"]] == ["TWO"]
    assert changed["blocks"][0]["ref"] == first["blocks"][1]["ref"]
    assert changed["changed_count"] == 1


def test_block_cursor_continues_a_large_block_and_is_document_version_bound(tmp_path):
    store = ContentStore(tmp_path)
    cache = SemanticPageCache(store, Journal(None), "a")
    first = cache.render(_raw("abcdefghij", "tail"), max_chars=4, max_links=10)
    assert first["blocks"][0]["text"] == "abcd" and first["cursor"]
    second = cache.render(_raw("abcdefghij", "tail"), max_chars=20, max_links=10,
                          cursor=first["cursor"])
    assert second["blocks"][0]["text"] == "efghij"
    assert second["blocks"][1]["text"] == "tail"

    with pytest.raises(DocumentVersionStale) as error:
        cache.render(_raw("changed", document_id=2), max_chars=20, max_links=10,
                     cursor=first["cursor"])
    assert error.value.cls is Class.DOCUMENT_VERSION_STALE
    fresh = cache.render(_raw("changed", document_id=2), max_chars=20, max_links=10)
    assert [block["text"] for block in fresh["blocks"]] == ["changed"]


def test_full_semantic_value_is_retrievable_by_content_digest(tmp_path):
    store = ContentStore(tmp_path)
    cache = SemanticPageCache(store, Journal(None), "a")
    out = cache.render(_raw("x" * 10_000), max_chars=10, max_links=10)
    complete = store.get(out["content_digest"])

    assert complete["blocks"][0]["text"] == "x" * 10_000
    assert out["text_truncated"] is True and len(out["text"]) == 10


def test_malformed_cursor_is_a_typed_version_failure(tmp_path):
    cache = SemanticPageCache(ContentStore(tmp_path), Journal(None), "a")
    with pytest.raises(DocumentVersionStale):
        cache.render(_raw("one"), max_chars=10, max_links=10, cursor="characters:20")


def test_link_metadata_change_versions_the_document_without_replaying_prose(tmp_path):
    cache = SemanticPageCache(ContentStore(tmp_path), Journal(None), "a")
    first_raw = _raw("one")
    first_raw["links"] = [{"text": "next", "href": "https://example.test/2"}]
    first = cache.render(first_raw, max_chars=100, max_links=10)
    second_raw = _raw("one")
    second_raw["links"] = [{"text": "next", "href": "https://example.test/3"}]
    second = cache.render(second_raw, max_chars=100, max_links=10)

    assert second["document_version"] != first["document_version"]
    assert second["blocks"] == [] and second["text"] == ""
    assert second["links"][0]["href"].endswith("/3")
