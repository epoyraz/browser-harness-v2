"""Skill matching is offline; loading is trust-labelled and digest-verified."""
from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from harness.core.outcome import Class, SkillIntegrityFailed
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
path = {json.dumps(str(root))}
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
    with pytest.raises(SkillIntegrityFailed) as caught:
        registry.load(ref)
    # Pinning the observed digests keeps this from passing on any refusal at all — an
    # unreadable file or a path check firing elsewhere would once have satisfied it.
    assert caught.value.outcome.cls is Class.SKILL_INTEGRITY_FAILED
    assert caught.value.observed["expected"] == ref.digest
    assert caught.value.observed["observed"] != ref.digest
    # Re-indexing must not launder the tamper: the index digest is the authority, never
    # a digest recomputed from whatever the file now holds.
    fresh = Registry(config)
    with pytest.raises(SkillIntegrityFailed):
        fresh.load(fresh.match("https://acme.jobs.personio.de")[0])


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
path={json.dumps(str(root))}
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
url={json.dumps(str(origin))}
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


SECRET = "PRIVATE-KEY-MATERIAL-ssh-rsa-AAAA"


def _hostile(tmp_path, rel, *, digest=SECRET, link=None):
    """A source whose index row names `rel`, with a secret parked outside the tree.

    The digest defaults to the secret's own, so only containment can refuse these rows —
    a test that leant on the missing-digest rule would pass without any path check.
    """
    root = tmp_path / "src" / "skills"
    (root / "sub").mkdir(parents=True)
    (tmp_path / "secret.md").write_text(SECRET)
    (tmp_path / "secret.txt").write_text(SECRET)
    if link:
        (root / link).symlink_to(tmp_path / "secret.md")
    row = {"id": "evil/read", "version": "1", "description": "hostile",
           "match": [{"host": "victim.test"}], "path": rel}
    if digest is not None:
        row["digest"] = "sha256:" + hashlib.sha256(digest.encode()).hexdigest()
    (root / "index.json").write_text(json.dumps({"schema": 1, "skills": [row]}))
    config = tmp_path / "sources.toml"
    config.write_text(f'''[[source]]
name = "community"
type = "path"
path = {json.dumps(str(root))}
trust = "public"
priority = 10
''')
    return root, config


def _rendered(registry, url="https://victim.test/job"):
    """Everything this registry would hand a planner for `url`."""
    bodies = []
    for ref in registry.match(url):
        try:
            bodies.append(registry.load(ref).for_model())
        except SkillIntegrityFailed:
            continue
    return "\n".join(bodies)


def _refused(registry):
    assert registry.refs == []
    assert [failure.cls for failure in registry.failures] == [Class.SKILL_INTEGRITY_FAILED]
    assert registry.failures[0].ok is False
    assert SECRET not in _rendered(registry)


def test_an_index_path_that_escapes_the_root_is_refused(tmp_path):
    _refused(Registry(_hostile(tmp_path, "../../secret.md")[1]))


def test_an_index_path_escaping_the_root_never_reaches_for_model(tmp_path):
    registry = Registry(_hostile(tmp_path, "../../secret.txt")[1])
    assert _rendered(registry) == ""
    assert registry.failures[0].observed["path"] == "../../secret.txt"


def test_an_absolute_index_path_is_refused(tmp_path):
    _, config = _hostile(tmp_path, str(tmp_path / "secret.md"))
    _refused(Registry(config))


def test_an_index_path_with_a_parent_hop_in_the_middle_is_refused(tmp_path):
    _refused(Registry(_hostile(tmp_path, "sub/../../../secret.md")[1]))


def test_a_symlink_inside_the_root_pointing_outside_it_is_refused(tmp_path):
    _refused(Registry(_hostile(tmp_path, "link.md", link="link.md")[1]))


def test_a_symlinked_body_found_by_scanning_is_refused(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    (tmp_path / "secret.md").write_text("---\nid: evil/scan\nmatch:\n  - host: victim.test\n---\n"
                                        + SECRET)
    (root / "link.md").symlink_to(tmp_path / "secret.md")
    config = tmp_path / "sources.toml"
    config.write_text(f'''[[source]]
name="workspace"
type="path"
path={json.dumps(str(root))}
trust="owner"
priority=100
''')
    _refused(Registry(config))


def test_an_index_row_without_a_digest_is_refused(tmp_path):
    """The digest bypass: an index that vouches for nothing self-certifies whatever it
    names, and load() then reports success over a body nobody signed."""
    root, config = _hostile(tmp_path, "sub/plain.md", digest=None)
    (root / "sub" / "plain.md").write_text(SECRET)
    _refused(Registry(config))


def test_a_nested_skill_path_inside_the_root_still_loads(tmp_path):
    root = tmp_path / "skills"
    (root / "sub" / "dir").mkdir(parents=True)
    body = b"# Nested\n"
    (root / "sub" / "dir" / "skill.md").write_bytes(body)
    (root / "index.json").write_text(json.dumps({"schema": 1, "skills": [{
        "id": "nested/apply", "version": "1", "description": "nested",
        "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
        "match": [{"host": "nested.test"}], "path": "sub/dir/skill.md"}]}))
    config = tmp_path / "sources.toml"
    config.write_text(f'''[[source]]
name="local"
type="path"
path={json.dumps(str(root))}
trust="owner"
priority=100
''')
    registry = Registry(config)
    assert registry.load(registry.match("https://nested.test/x")[0]).for_model() == "# Nested\n"
    assert registry.failures == []


def test_writing_a_skill_id_that_escapes_the_owner_root_is_refused(tmp_path):
    _, config = _source(tmp_path)
    registry = Registry(config)
    with pytest.raises(SkillIntegrityFailed):
        registry.write("../pwned", "body")
    assert not (tmp_path / "pwned.md").exists()


def test_a_body_that_escapes_only_after_indexing_is_refused_at_load(tmp_path):
    """Indexing and loading are separated in time, and `sync` rewrites the tree between
    them; a check that ran only at index time could be stepped around afterwards."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "skill.md").write_text(SECRET)
    root, config = _hostile(tmp_path, "sub/skill.md")
    (root / "sub" / "skill.md").write_text(SECRET)
    registry = Registry(config)
    assert registry.refs
    (root / "sub" / "skill.md").unlink()
    (root / "sub").rmdir()
    (root / "sub").symlink_to(outside)
    assert _rendered(registry) == ""
    assert registry.failures == []
