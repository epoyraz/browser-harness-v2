"""Lossless, content-addressed elision for agent-facing values.

The browser can return megabytes from one otherwise successful call.  Truncating that data
silently is a correctness bug; printing it verbatim is an agent-context failure.  This module
keeps the two concerns separate: :class:`ContentStore` persists the exact typed value under a
SHA-256 digest, while :class:`OutputCapture` replaces only over-budget stdout with a compact,
reversible marker.

Stored payloads are deliberately not journalled.  Page text, JavaScript results, form values,
and headers may be sensitive; journals receive counts and digests from their callers instead.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, TextIO

DEFAULT_OUTPUT_BYTES = 128_000
DEFAULT_VALUE_BYTES = 128_000
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _encode(value: Any) -> tuple[str, bytes]:
    if isinstance(value, bytes):
        return "bytes", value
    if isinstance(value, str):
        return "text", value.encode("utf-8")
    return "json", json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")


def _decode(kind: str, payload: bytes) -> Any:
    if kind == "bytes":
        return payload
    if kind == "text":
        return payload.decode("utf-8")
    if kind == "json":
        return json.loads(payload)
    raise ValueError(f"unknown content kind {kind!r}")


def _preview(kind: str, payload: bytes, chars: int = 240) -> tuple[str, str]:
    if kind == "bytes":
        text = base64.b64encode(payload).decode("ascii")
        prefix = "base64:"
    else:
        text = payload.decode("utf-8", errors="replace")
        prefix = ""
    if len(text) <= chars * 2:
        return prefix + text, ""
    return prefix + text[:chars], text[-chars:]


@dataclass(frozen=True, slots=True)
class ContentRef:
    digest: str
    bytes: int
    kind: str
    head: str
    tail: str

    def marker(self, *, surface: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "_elided": self.bytes,
            "_sha256": self.digest,
            "_kind": self.kind,
            "head": self.head,
            "tail": self.tail,
            "retrieval": f"fetch_content('{self.digest}')",
        }
        if surface:
            out["surface"] = surface
        return out


class ContentStore:
    """Small persistent blob store keyed by the exact typed payload digest."""

    def __init__(self, root: str | os.PathLike[str] | None = None):
        configured = root or os.environ.get("BH_CONTENT_STORE")
        self.root = Path(configured).expanduser() if configured else (
            Path.home() / ".cache" / "browser-harness" / "content"
        )
        self._lock = threading.Lock()

    def put(self, value: Any) -> ContentRef:
        kind, payload = _encode(value)
        digest = sha256(kind.encode("ascii") + b"\0" + payload).hexdigest()
        head, tail = _preview(kind, payload)
        destination = self.root / digest[:2] / digest[2:]
        with self._lock:
            # Blobs may contain rendered page text or form state. Keep both new and
            # pre-existing store directories private even when the process umask would
            # otherwise leave them world-readable; mkstemp creates each blob as 0600.
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.root, 0o700)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(destination.parent, 0o700)
            if not destination.exists():
                fd, temporary = tempfile.mkstemp(
                    prefix=f".{digest[:12]}-", dir=destination.parent,
                )
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(kind.encode("ascii") + b"\n" + payload)
                    os.replace(temporary, destination)
                finally:
                    try:
                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass
        return ContentRef(digest=digest, bytes=len(payload), kind=kind, head=head, tail=tail)

    def get(self, digest: str) -> Any:
        if not _DIGEST.fullmatch(str(digest)):
            raise ValueError("content digest must be 64 lowercase hexadecimal characters")
        path = self.root / digest[:2] / digest[2:]
        raw = path.read_bytes()
        kind_raw, separator, payload = raw.partition(b"\n")
        if not separator:
            raise ValueError("content blob has no type header")
        kind = kind_raw.decode("ascii")
        actual = sha256(kind_raw + b"\0" + payload).hexdigest()
        if actual != digest:
            raise ValueError("content blob digest mismatch")
        return _decode(kind, payload)

    def elide(self, value: Any, *, limit: int = DEFAULT_VALUE_BYTES,
              surface: str | None = None) -> Any:
        _kind, payload = _encode(value)
        if len(payload) <= max(0, int(limit)):
            return value
        return self.put(value).marker(surface=surface)


class OutputCapture(io.TextIOBase):
    """Invocation-wide stdout buffer with one reversible byte ceiling.

    Buffering makes the ceiling apply to the actual transcript, not to arbitrary `write()`
    fragments.  `json.dump`, `print(a, b)`, and many small prints therefore produce one exact
    retrievable value rather than a trail of independently elided chunks.
    """

    def __init__(self, target: TextIO, store: ContentStore, *, limit: int):
        super().__init__()
        self.target = target
        self.store = store
        self.limit = max(0, int(limit))
        self._parts: list[str] = []
        self._emitted = False
        self.stats: dict[str, Any] = {
            "output_bytes": 0, "output_emitted_bytes": 0, "output_truncated": False,
        }

    @property
    def encoding(self) -> str:  # pragma: no cover - trivial compatibility surface
        return getattr(self.target, "encoding", None) or "utf-8"

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed output capture")
        value = str(text)
        self._parts.append(value)
        return len(value)

    def flush(self) -> None:
        # The real stream is flushed when the complete, possibly-elided value is emitted.
        return None

    def emit(self) -> dict[str, Any]:
        if self._emitted:
            return dict(self.stats)
        self._emitted = True
        value = "".join(self._parts)
        encoded = value.encode("utf-8")
        self.stats["output_bytes"] = len(encoded)
        if len(encoded) <= self.limit:
            rendered = value
        else:
            ref = self.store.put(value)
            marker = ref.marker(surface="stdout")
            rendered = json.dumps(marker, ensure_ascii=False, sort_keys=True) + "\n"
            self.stats.update({
                "output_truncated": True,
                "output_digest": ref.digest,
                "output_spilled_bytes": ref.bytes,
            })
        self.stats["output_emitted_bytes"] = len(rendered.encode("utf-8"))
        self.target.write(rendered)
        self.target.flush()
        return dict(self.stats)
