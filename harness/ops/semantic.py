"""Versioned semantic page blocks and stable continuation cursors.

Chrome supplies the DOM facts; this module performs only mechanical shaping: stable refs from
structural keys, content digests, version comparison, bounded windows, and cursor validation.
It deliberately makes no claim about what a page *means* or what the model should do next.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any

from harness.core.content import ContentStore
from harness.core.journal import Journal
from harness.core.outcome import DocumentVersionStale

_CURSOR = re.compile(r"^sb1\.([0-9a-f]{64})\.([ad])\.(\d+)\.(\d+)$")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _normal_text(value: Any) -> str:
    return "\n".join(line.rstrip() for line in str(value or "").replace("\r", "").splitlines()) \
        .strip()


def _integer(value: Any, fallback: int = 0) -> int:
    try:
        return fallback if value is None else int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback


@dataclass(slots=True)
class _Document:
    version: str
    blocks: list[dict[str, Any]]
    emission_indices: list[int]
    removed_refs: list[str]
    content_digest: str
    meta: dict[str, Any]


class SemanticPageCache:
    """Per-tab cache.  It never crosses browser users, origins, or Session instances."""

    def __init__(self, store: ContentStore, journal: Journal, target_id: str):
        self.store = store
        self.journal = journal
        self.target_id = target_id
        self._documents: deque[_Document] = deque(maxlen=4)
        self._lock = threading.Lock()

    @property
    def latest(self) -> _Document | None:
        return self._documents[-1] if self._documents else None

    def _make_document(self, raw: dict[str, Any]) -> _Document:
        blocks: list[dict[str, Any]] = []
        keys: dict[str, int] = {}
        for position, source in enumerate(raw.get("blocks") or []):
            if not isinstance(source, dict):
                continue
            base_key = str(source.get("key") or f"block:{position}")
            occurrence = keys.get(base_key, 0)
            keys[base_key] = occurrence + 1
            key = base_key if occurrence == 0 else f"{base_key}~{occurrence}"
            payload: dict[str, Any] = {
                "kind": str(source.get("kind") or "region"),
                "text": _normal_text(source.get("text")),
            }
            for field in ("level", "links", "control", "state",
                          "text_chars", "text_truncated"):
                if source.get(field) is not None:
                    payload[field] = source[field]
            block_digest = _digest(payload)
            blocks.append({
                "ref": "b" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16],
                "key": key,
                "digest": block_digest,
                **payload,
            })

        identity = {
            "document_id": raw.get("document_id"),
            "url": raw.get("url"),
            "title": raw.get("title"),
            "blocks": [(block["key"], block["digest"]) for block in blocks],
            # Standalone links and challenge evidence remain metadata rather than prose
            # blocks, but a cursor must still go stale when either materially changes.
            "links": raw.get("links") or [],
            "challenge": raw.get("challenge") or {},
            "blocks_truncated": bool(raw.get("blocks_truncated")),
            "block_candidates": _integer(raw.get("block_candidates"), len(blocks)),
            "links_truncated": bool(raw.get("links_truncated")),
            "link_candidates": _integer(raw.get("link_candidates")),
        }
        version = _digest(identity)
        previous = self.latest
        if previous is None or previous.version == version:
            changed = list(range(len(blocks))) if previous is None else []
            removed: list[str] = []
        else:
            old = {block["key"]: block for block in previous.blocks}
            new_keys = {block["key"] for block in blocks}
            changed = [i for i, block in enumerate(blocks)
                       if block["key"] not in old
                       or old[block["key"]]["digest"] != block["digest"]]
            removed = [block["ref"] for block in previous.blocks
                       if block["key"] not in new_keys]
        meta = {key: raw.get(key) for key in (
            "url", "title", "ready_state", "language", "links", "challenge",
            "text_chars", "block_chars", "block_candidates", "blocks_truncated",
            "link_candidates", "links_truncated",
            # Rendering evidence: what the document holds regardless of the text budget,
            # and the hidden-tab verdict a caller must not mistake for an empty page.
            "rendered", "blank_while_hidden", "hint",
        )}
        complete = {**meta, "document_version": version, "blocks": blocks}
        content_ref = self.store.put(complete)
        return _Document(version=version, blocks=blocks, emission_indices=changed,
                         removed_refs=removed, content_digest=content_ref.digest, meta=meta)

    @staticmethod
    def _cursor(version: str, mode: str, position: int, offset: int) -> str:
        return f"sb1.{version}.{mode}.{position}.{offset}"

    @staticmethod
    def _parse_cursor(cursor: str) -> tuple[str, str, int, int]:
        found = _CURSOR.fullmatch(str(cursor))
        if found is None:
            raise DocumentVersionStale("malformed semantic block cursor", cursor=cursor)
        return found.group(1), found.group(2), int(found.group(3)), int(found.group(4))

    def _window(self, document: _Document, indices: list[int], *, max_chars: int,
                mode: str, position: int = 0, offset: int = 0) -> tuple[
                    list[dict[str, Any]], str | None, int]:
        limit = max(0, min(int(max_chars), 100_000))
        emitted: list[dict[str, Any]] = []
        used = 0
        pos = max(0, position)
        inner = max(0, offset)
        while pos < len(indices):
            block = document.blocks[indices[pos]]
            text = str(block.get("text") or "")
            available = max(0, len(text) - inner)
            remaining = max(0, limit - used)
            if available and remaining == 0:
                break
            take = min(available, remaining)
            piece = {key: value for key, value in block.items() if key != "key"}
            piece["text"] = text[inner:inner + take]
            if inner or take < available:
                piece["text_start"] = inner
                piece["text_truncated"] = inner + take < len(text)
            emitted.append(piece)
            used += take
            if take < available:
                inner += take
                break
            pos += 1
            inner = 0
        cursor = (self._cursor(document.version, mode, pos, inner)
                  if pos < len(indices) else None)
        remaining_chars = 0
        if pos < len(indices):
            remaining_chars += max(0, len(str(document.blocks[indices[pos]].get("text") or ""))
                                   - inner)
            remaining_chars += sum(len(str(document.blocks[index].get("text") or ""))
                                   for index in indices[pos + 1:])
        return emitted, cursor, remaining_chars

    def render(self, raw: dict[str, Any], *, max_chars: int, max_links: int,
               cursor: str | None = None, start: int = 0) -> dict[str, Any]:
        """Shape one raw browser extraction without ever journalling its content."""
        if not isinstance(raw.get("blocks"), list):
            # Compatibility for a recorded/fake pre-block payload. Live evaluations from
            # this version always contain `blocks`.
            return raw
        with self._lock:
            document = self._make_document(raw)
            parsed_cursor: tuple[str, str, int, int] | None = None
            if cursor is not None:
                parsed_cursor = self._parse_cursor(cursor)
                if parsed_cursor[0] != document.version:
                    # A failed continuation must not consume the new document's delta.
                    # The caller can recover with one cursor-free read and still receive
                    # the blocks that changed since its last accepted generation.
                    raise DocumentVersionStale(
                        "semantic block cursor belongs to another document version",
                        cursor_version=parsed_cursor[0],
                        document_version=document.version,
                        target_id=self.target_id,
                    )
            same = self.latest is not None and self.latest.version == document.version
            if not same:
                self._documents.append(document)
            else:
                # Preserve the original emission set so an already-issued cursor remains
                # stable even after an unchanged read returns references only.
                document = self.latest
                assert document is not None

            if cursor is not None:
                assert parsed_cursor is not None
                _version, mode, position, offset = parsed_cursor
                indices = (list(range(len(document.blocks))) if mode == "a"
                           else list(document.emission_indices))
            elif start:
                # Legacy offsets remain supported, but are explicitly not cursors: callers
                # that need mutation safety should continue with the returned block cursor.
                semantic_chars = sum(len(str(block.get("text") or ""))
                                     for block in document.blocks)
                text = str(raw.get("text") or "")
                out = {**document.meta, "text": text,
                       "text_chars": _integer(raw.get("text_chars"), len(text)),
                       "text_start": _integer(raw.get("text_start"), max(0, int(start))),
                       "text_remaining": _integer(raw.get("text_remaining")),
                       "text_truncated": bool(raw.get("text_truncated")),
                       "blocks": [], "document_version": document.version,
                       "content_digest": document.content_digest,
                       "semantic_text_chars": semantic_chars,
                       "block_count": len(document.blocks), "cursor": None}
                self._note(document, out, repeated=0)
                return out
            elif same:
                repeated = sum(len(str(block.get("text") or "").encode("utf-8"))
                               for block in document.blocks)
                refs = [block["ref"] for block in document.blocks]
                semantic_chars = sum(
                    len(str(block.get("text") or "")) for block in document.blocks)
                out = {**document.meta, "text": "",
                       "text_chars": _integer(
                           document.meta.get("text_chars"), semantic_chars),
                       "semantic_text_chars": semantic_chars,
                       "text_start": 0, "text_remaining": 0, "text_truncated": False,
                       "blocks": [], "document_version": document.version,
                       "content_digest": document.content_digest,
                       "block_count": len(document.blocks), "changed_count": 0,
                       "unchanged_refs": refs[:500], "unchanged_count": len(refs),
                       "repeated_output_bytes": repeated, "removed_refs": [],
                       "cursor": None}
                self._note(document, out, repeated=repeated)
                return out
            else:
                mode, position, offset = "d", 0, 0
                indices = list(document.emission_indices)

            emitted, next_cursor, remaining = self._window(
                document, indices, max_chars=max_chars, mode=mode,
                position=position, offset=offset,
            )
            text = "\n\n".join(str(block.get("text") or "") for block in emitted)
            out = {
                **document.meta,
                "links": list(document.meta.get("links") or [])[:max(0, int(max_links))],
                "text": text,
                "text_chars": _integer(document.meta.get("text_chars"), sum(
                    len(str(block.get("text") or "")) for block in document.blocks)),
                "semantic_text_chars": sum(len(str(block.get("text") or ""))
                                           for block in document.blocks),
                "text_start": 0,
                "text_remaining": remaining,
                "text_truncated": next_cursor is not None
                                  or bool(document.meta.get("blocks_truncated")),
                "blocks": emitted,
                "document_version": document.version,
                "content_digest": document.content_digest,
                "block_count": len(document.blocks),
                "changed_count": len(document.emission_indices),
                "removed_refs": list(document.removed_refs),
                "cursor": next_cursor,
            }
            self._note(document, out, repeated=0)
            return out

    def _note(self, document: _Document, output: dict[str, Any], *, repeated: int) -> None:
        self.journal.write(
            "note", event="semantic_digest", target_id=self.target_id,
            document_version=document.version, content_digest=document.content_digest,
            block_count=len(document.blocks), emitted_blocks=len(output.get("blocks") or []),
            changed_blocks=int(output.get("changed_count") or 0),
            block_digests=[
                {"ref": block.get("ref"), "digest": block.get("digest")}
                for block in document.blocks[:500]
                if isinstance(block, dict)
            ],
            block_digests_truncated=len(document.blocks) > 500,
            removed_blocks=len(output.get("removed_refs") or []),
            truncated=bool(output.get("text_truncated")),
            extraction_truncated=bool(document.meta.get("blocks_truncated")),
            repeated_output_bytes=repeated,
        )
