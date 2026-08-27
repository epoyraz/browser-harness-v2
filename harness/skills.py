"""Offline skill matching with versioned sources, trust tiers, and digest checks."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any
from urllib.parse import urlsplit

from harness.core.outcome import Class, Outcome, SkillIntegrityFailed, fail

TRUST = frozenset({"owner", "team", "public"})


@dataclass(frozen=True, slots=True)
class Source:
    name: str
    type: str
    trust: str
    priority: int
    location: str
    ref: str = "main"


@dataclass(frozen=True, slots=True)
class SkillRef:
    id: str
    version: str
    description: str
    digest: str
    source: str
    trust: str
    priority: int
    path: Path
    matcher: dict[str, str]

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "version": self.version, "description": self.description,
                "source": self.source, "trust": self.trust, "priority": self.priority,
                "digest": self.digest, "path": str(self.path), "matcher": self.matcher}


@dataclass(frozen=True, slots=True)
class SkillBody:
    ref: SkillRef
    content: str

    def for_model(self) -> str:
        if self.ref.trust != "public":
            return self.content
        return ("<untrusted-skill-reference source=\"" + self.ref.source + "\" id=\""
                + self.ref.id + "\">\n" + self.content
                + "\n</untrusted-skill-reference>")


def _config_root() -> Path:
    raw = os.environ.get("BH_CONFIG_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".config" / "browser-harness"


def _cache_root() -> Path:
    raw = os.environ.get("BH_CACHE_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".cache" / "browser-harness"


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _body_path(root: Path, rel: str) -> Path | None:
    """The single gate every skill body path passes through. None means refused.

    An index is data from a party we may not trust: `bh skills sync` clones public Git
    sources, and `Session.run_application()` splices matching bodies straight into the
    planner's model_context. A row naming `../../.ssh/id_rsa` therefore turns a hostile
    index into an exfiltration primitive — the secret lands in the model's context and
    from there anywhere the agent writes.

    Resolving before comparing, rather than testing the string for `..`, is the point: a
    symlink *inside* the tree pointing out of it satisfies any lexical check. The lexical
    rejections still come first, because `root / "/etc/passwd"` discards `root` entirely
    and a resolved `..` chain that lands back inside the root is still a row lying about
    what it reads.
    """
    pure = PurePath(rel)
    if not rel or pure.is_absolute() or ".." in pure.parts or pure.suffix != ".md":
        return None
    path = root / pure
    try:
        if not path.resolve().is_relative_to(root.resolve()):
            return None
    except (OSError, RuntimeError):  # unreadable component, or a symlink loop
        return None
    return path


def _scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    data: dict[str, Any] = {"match": []}
    in_match = False
    for raw in text[4:end].splitlines():
        line = raw.strip()
        if line == "match:":
            in_match = True
            continue
        hit = re.match(r"-\s*(host|url|detect):\s*(.+)$", line) if in_match else None
        if hit:
            data["match"].append({hit.group(1): _scalar(hit.group(2))})
            continue
        if line.startswith("-") or not line:
            continue
        in_match = False
        if ":" in line:
            key, value = line.split(":", 1)
            if key in {"id", "version", "description", "digest"}:
                data[key] = _scalar(value)
    return data


class Registry:
    """Indexes local sources once; hot-path matching performs no I/O or browser calls."""

    def __init__(self, config: str | os.PathLike[str] | None = None):
        self.config = Path(config).expanduser() if config else Path(
            os.environ.get("BH_SKILLS_SOURCES") or _config_root() / "sources.toml")
        self.sources = self._sources()
        #: Rows refused while indexing, still typed (outcome rule 4): a dropped skill is
        #: not a skill that never existed, and a refusal here is evidence of an index
        #: trying to read outside its own tree.
        self.failures: list[Outcome] = []
        self.refs = self._index()

    def _sources(self) -> list[Source]:
        try:
            data = tomllib.loads(self.config.read_text(encoding="utf-8"))
            rows = data.get("source") or []
        except OSError:
            rows = [{"name": "workspace", "type": "path", "trust": "owner",
                     "priority": 100,
                     "path": str(_config_root() / "workspace" / "skills")}]
        sources = []
        for row in rows:
            kind = str(row.get("type") or "path")
            trust = str(row.get("trust") or "public")
            if kind not in {"path", "git"} or trust not in TRUST:
                continue
            location = str(row.get("path") if kind == "path" else row.get("url") or "")
            sources.append(Source(str(row.get("name") or kind), kind, trust,
                                  int(row.get("priority") or 0), location,
                                  str(row.get("ref") or "main")))
        return sorted(sources, key=lambda source: source.priority, reverse=True)

    def _root(self, source: Source) -> Path:
        if source.type == "path":
            return Path(source.location).expanduser()
        return _cache_root() / "skills" / source.name

    def _refuse(self, source: Source, detail: str, **observed: Any) -> None:
        self.failures.append(fail(Class.SKILL_INTEGRITY_FAILED, detail,
                                  source=source.name, **observed))

    def _index(self) -> list[SkillRef]:
        refs = []
        self.failures = []
        for source in self.sources:
            root = self._root(source)
            index = root / "index.json"
            if index.is_file():
                try:
                    rows = json.loads(index.read_text(encoding="utf-8")).get("skills") or []
                except (OSError, ValueError, AttributeError):
                    rows = []
                for row in rows:
                    rel = str(row.get("path") or (str(row.get("id")) + ".md"))
                    skill = str(row.get("id") or "")
                    path = _body_path(root, rel)
                    if path is None:
                        self._refuse(source, "indexed skill path escapes its source root",
                                     skill=skill, path=rel)
                        continue
                    # An index row with no digest verifies the body against itself, so
                    # load() would report success over a body nobody ever vouched for —
                    # the same silent pass as the escape above, one field further on.
                    if not str(row.get("digest") or "").strip():
                        self._refuse(source, "indexed skill declares no digest to verify",
                                     skill=skill, path=rel)
                        continue
                    refs.extend(self._refs_for(source, path, row))
                continue
            for path in root.rglob("*.md") if root.is_dir() else []:
                # rglob will happily hand back a symlinked `*.md` aimed out of the tree.
                if _body_path(root, str(path.relative_to(root))) is None:
                    self._refuse(source, "scanned skill body escapes its source root",
                                 path=str(path))
                    continue
                try:
                    meta = _frontmatter(path.read_text(encoding="utf-8"))
                except OSError:
                    continue
                if meta.get("id"):
                    refs.extend(self._refs_for(source, path, meta))
        return sorted(refs, key=lambda ref: (ref.priority, ref.id, ref.version), reverse=True)

    def _contained(self, ref: SkillRef) -> bool:
        """Re-check at load time: indexing and loading are separated by arbitrary time,
        and `sync` rewrites the tree in between — a directory swapped for a symlink after
        indexing would otherwise be read without ever passing the gate."""
        source = next((item for item in self.sources if item.name == ref.source), None)
        if source is None:
            return False
        root = self._root(source)
        try:
            rel = ref.path.relative_to(root)
        except ValueError:
            return False
        return _body_path(root, str(rel)) is not None

    def _refs_for(self, source: Source, path: Path, row: dict[str, Any]) -> list[SkillRef]:
        try:
            # Only reached for bodies discovered by scanning, where the file *is* the
            # authority; indexed rows without a digest are refused before they get here.
            digest = str(row.get("digest") or _digest(path.read_bytes()))
        except OSError:
            digest = str(row.get("digest") or "")
        common = {"id": str(row.get("id") or ""),
                  "version": str(row.get("version") or "0"),
                  "description": str(row.get("description") or ""),
                  "digest": digest, "source": source.name, "trust": source.trust,
                  "priority": source.priority, "path": path}
        return [SkillRef(**common, matcher=dict(matcher))
                for matcher in row.get("match") or [] if isinstance(matcher, dict)]

    def match(self, url: str) -> list[SkillRef]:
        host = (urlsplit(url).hostname or "").lower()
        matches = []
        seen = set()
        for ref in self.refs:
            matcher = ref.matcher
            hit = ("host" in matcher and fnmatch.fnmatch(host, matcher["host"].lower())) \
                or ("url" in matcher and re.search(matcher["url"], url) is not None)
            key = (ref.source, ref.id, ref.version)
            if hit and key not in seen:
                matches.append(ref)
                seen.add(key)
        return matches

    def load(self, ref: SkillRef) -> SkillBody:
        if not self._contained(ref):
            raise SkillIntegrityFailed("skill body lies outside its source root",
                                       skill=ref.id, source=ref.source, path=str(ref.path))
        try:
            body = ref.path.read_bytes()
        except OSError as error:
            raise SkillIntegrityFailed("indexed skill body is unavailable", skill=ref.id,
                                       source=ref.source, path=str(ref.path)) from error
        got = _digest(body)
        if not ref.digest or got != ref.digest:
            raise SkillIntegrityFailed("skill body digest does not match its index",
                                       skill=ref.id, source=ref.source,
                                       expected=ref.digest, observed=got)
        return SkillBody(ref, body.decode("utf-8"))

    def search(self, query: str) -> list[SkillRef]:
        needle = query.casefold()
        unique: dict[tuple[str, str, str], SkillRef] = {}
        for ref in self.refs:
            key = (ref.source, ref.id, ref.version)
            if needle in (ref.id + " " + ref.description).casefold():
                unique.setdefault(key, ref)
        return list(unique.values())

    def sync(self, source_name: str | None = None) -> list[dict[str, Any]]:
        reports = []
        for source in self.sources:
            if source.type != "git" or (source_name and source.name != source_name):
                continue
            root = self._root(source)
            root.parent.mkdir(parents=True, exist_ok=True)
            if (root / ".git").is_dir():
                command = ["git", "-C", str(root), "fetch", "--depth", "1", "origin", source.ref]
                subprocess.run(command, check=True, capture_output=True, text=True)
                subprocess.run(["git", "-C", str(root), "reset", "--hard", "FETCH_HEAD"],
                               check=True, capture_output=True, text=True)
            else:
                subprocess.run(["git", "clone", "--depth", "1", "--branch", source.ref,
                                source.location, str(root)], check=True,
                               capture_output=True, text=True)
            reports.append({"source": source.name, "path": str(root), "ok": True})
        self.refs = self._index()
        return reports

    def record(self, ref: SkillRef, ok: bool, note: str = "") -> None:
        path = _config_root() / "skill-outcomes.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"ts": round(time.time(), 3), "id": ref.id,
                                     "source": ref.source, "ok": bool(ok),
                                     "note": note[:500]}) + "\n")

    def write(self, skill_id: str, body: str) -> Path:
        source = next((item for item in self.sources
                       if item.type == "path" and item.trust == "owner"), None)
        if source is None:
            raise ValueError("no owner path source is configured")
        # An agent-supplied id is no more trustworthy than an index row: `../../.zshrc`
        # would make this a write primitive aimed anywhere on the machine.
        root = self._root(source)
        path = _body_path(root, skill_id + ".md")
        if path is None:
            raise SkillIntegrityFailed("skill id escapes its source root",
                                       skill=skill_id, source=source.name, root=str(root))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        self.refs = self._index()
        return path

