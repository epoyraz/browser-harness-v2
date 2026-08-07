"""Skill matching is offline; loading is trust-labelled and digest-verified."""
from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from harness.core.outcome import SkillIntegrityFailed
from harness.skills import Registry


def _source(tmp_path, *, trust="owner"):
    root = tmp_path / "skills"
    root.mkdir()
    body = b"# Personio\n\nUse the application schema.\n"
    (root / "personio.md").write_bytes(body)
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    (root / "index.json").write_text(json.dumps({"schema": 1, "skills": [{
        "id": "personio/apply", "version": "3.2.0", "description": "Personio forms",
        "match": [{"host": "*.jobs.personio.de"}], "digest": digest,
        "path": "personio.md"}]}))
    config = tmp_path / "sources.toml"
    config.write_text(f'''[[source]]
name = "local"
type = "path"
path = "{root}"
trust = "{trust}"
priority = 100
''')
    return root, config


def test_host_matching_uses_only_the_local_index(tmp_path):
    _, config = _source(tmp_path)
    registry = Registry(config)
    got = registry.match("https://acme.jobs.personio.de/job/1")
    assert [(ref.id, ref.matcher) for ref in got] == [
        ("personio/apply", {"host": "*.jobs.personio.de"})]


def test_loading_verifies_the_index_digest(tmp_path):
    root, config = _source(tmp_path)
    registry = Registry(config)
    ref = registry.match("https://acme.jobs.personio.de")[0]
    (root / "personio.md").write_text("tampered")
    with pytest.raises(SkillIntegrityFailed):
        registry.load(ref)


def test_public_skill_body_is_delimited_as_untrusted_reference(tmp_path):
    _, config = _source(tmp_path, trust="public")
    registry = Registry(config)
    body = registry.load(registry.match("https://x.jobs.personio.de")[0])
    rendered = body.for_model()
    assert rendered.startswith("<untrusted-skill-reference")
    assert rendered.endswith("</untrusted-skill-reference>")


def test_path_source_can_scan_frontmatter_without_an_index(tmp_path):
    root = tmp_path / "skills"
    (root / "acme").mkdir(parents=True)
    (root / "acme" / "apply.md").write_text('''---
id: acme/apply
version: 1.0.0
description: Acme applications
match:
  - host: "careers.acme.test"
---
# Apply
''')
    config = tmp_path / "sources.toml"
    config.write_text(f'''[[source]]
name="workspace"
type="path"
path="{root}"
trust="owner"
priority=100
''')
    assert Registry(config).match("https://careers.acme.test/job")[0].id == "acme/apply"


def test_git_sync_clones_a_pinned_local_source(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=origin, check=True, capture_output=True)
    body = b"# Git skill\n"
    (origin / "git.md").write_bytes(body)
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    (origin / "index.json").write_text(json.dumps({"schema": 1, "skills": [{
        "id": "git/apply", "version": "1", "description": "git", "path": "git.md",
        "digest": digest, "match": [{"host": "git.test"}]}]}))
    subprocess.run(["git", "add", "."], cwd=origin, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.test",
                    "commit", "-m", "seed"], cwd=origin, check=True, capture_output=True)
    config = tmp_path / "sources.toml"
    config.write_text(f'''[[source]]
name="git-local"
type="git"
url="{origin}"
ref="main"
trust="team"
priority=50
''')
    monkeypatch.setenv("BH_CACHE_HOME", str(tmp_path / "cache"))
    registry = Registry(config)
    assert registry.match("https://git.test") == []
    assert registry.sync() == [{"source": "git-local",
                                "path": str(tmp_path / "cache" / "skills" / "git-local"),
                                "ok": True}]
    assert registry.match("https://git.test")[0].trust == "team"
