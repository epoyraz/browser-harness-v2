"""ATS credential metadata without touching the real macOS Keychain."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from harness import auth


def test_tenant_service_is_stable_per_host_and_optional_tenant():
    assert auth.tenant_service("https://ACME.example/jobs/1") == (
        "browser-harness:ats:acme.example")
    assert auth.tenant_service("https://jobs.example/apply?careerSite=Swiss") == (
        "browser-harness:ats:jobs.example:swiss")


def test_account_status_checks_only_the_explicit_service_and_account(monkeypatch):
    calls = []
    monkeypatch.setattr(auth.subprocess, "run", lambda command, **kwargs: (
        calls.append((command, kwargs)) or SimpleNamespace(returncode=0)))

    result = auth.account_credential_status("https://jobs.example/1", "me@example.test")

    assert result == {"service": "browser-harness:ats:jobs.example",
                      "account": "me@example.test", "stored": True}
    assert calls[0][0] == ["security", "find-generic-password", "-s",
                           "browser-harness:ats:jobs.example", "-a", "me@example.test"]


def test_existing_credential_is_reused_without_generating_or_returning_a_password(monkeypatch):
    monkeypatch.setattr(auth, "account_credential_status", lambda url, email: {
        "service": "browser-harness:ats:jobs.example", "account": email, "stored": True})
    monkeypatch.setattr(auth.secrets, "choice", lambda alphabet: pytest.fail(
        "an existing credential must not generate a new secret"))

    result = auth.ensure_account_credential("https://jobs.example", "me@example.test")

    assert result["created"] is False
    assert "password" not in result


def test_new_credential_enforces_length_and_returns_metadata_only(monkeypatch):
    status = {"service": "browser-harness:ats:jobs.example",
              "account": "me@example.test", "stored": False}
    monkeypatch.setattr(auth, "account_credential_status", lambda url, email: dict(status))
    with pytest.raises(ValueError, match="at least 16"):
        auth.ensure_account_credential("https://jobs.example", "me@example.test", length=15)

    commands = []
    monkeypatch.setattr(auth.secrets, "choice", lambda alphabet: alphabet[0])
    monkeypatch.setattr(auth.secrets, "SystemRandom", lambda: SimpleNamespace(
        shuffle=lambda values: None))
    monkeypatch.setattr(auth.subprocess, "run", lambda command, **kwargs: (
        commands.append(command) or SimpleNamespace(returncode=0)))

    result = auth.ensure_account_credential(
        "https://jobs.example", "me@example.test", length=16)

    assert result == {**status, "stored": True, "created": True}
    assert "password" not in result
    assert commands[0][:7] == ["security", "add-generic-password", "-a",
                               "me@example.test", "-s",
                               "browser-harness:ats:jobs.example", "-l"]
