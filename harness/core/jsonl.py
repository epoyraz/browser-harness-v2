"""Small, failure-tolerant JSONL reader shared by local artifact consumers."""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any


def files(paths: Iterable[str | Path]) -> list[Path]:
    """Expand files and directories without requiring every path to exist."""
    out: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            out.extend(path.rglob("*.jsonl"))
        elif path.is_file():
            out.append(path)
    return out


def read(path: str | Path, *, missing_ok: bool = True) -> Iterator[dict[str, Any]]:
    """Yield valid objects; malformed lines are skipped without hiding required files."""
    try:
        stream = Path(path).expanduser().open(encoding="utf-8")
    except OSError:
        if missing_ok:
            return
        raise
    with stream:
        for line in stream:
            try:
                value = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(value, dict):
                yield value
