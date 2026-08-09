"""Idempotent ATS credential registry backed by macOS Keychain.

Account creation is external state, so provisioning is deliberately separate from lookup.
No function in this module returns a password.
"""
from __future__ import annotations

import secrets
import string
import subprocess
from urllib.parse import parse_qs, urlsplit


def tenant_service(url: str) -> str:
    """Return a stable credential key for an ATS tenant, never an individual job."""
    parts = urlsplit(url)
    host = (parts.hostname or "unknown").lower()
    query = parse_qs(parts.query)
    tenant = next((query[key][0] for key in ("company", "tenant", "careerSite")
                   if query.get(key)), "")
    suffix = f":{tenant.lower()}" if tenant else ""
    return f"browser-harness:ats:{host}{suffix}"


def account_credential_status(url: str, email: str) -> dict[str, object]:
    """Check the explicit tenant/email key without enumerating the user's Keychain."""
    service = tenant_service(url)
    found = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", email],
        check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"service": service, "account": email, "stored": found.returncode == 0}


def ensure_account_credential(url: str, email: str, *, length: int = 28) -> dict[str, object]:
    """Create one strong tenant credential if absent; never replace an existing one."""
    status = account_credential_status(url, email)
    if status["stored"]:
        return {**status, "created": False}
    if length < 16:
        raise ValueError("account passwords must be at least 16 characters")
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    chars = [secrets.choice(string.ascii_uppercase), secrets.choice(string.ascii_lowercase),
             secrets.choice(string.digits), secrets.choice("!@#$%^&*-_=+")]
    chars.extend(secrets.choice(alphabet) for _ in range(length - len(chars)))
    secrets.SystemRandom().shuffle(chars)
    password = "".join(chars)
    try:
        added = subprocess.run(
            ["security", "add-generic-password", "-a", email,
             "-s", str(status["service"]), "-l", f"ATS account {email}", "-w", password],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        password = ""
    if added.returncode != 0:
        raced = account_credential_status(url, email)
        if raced["stored"]:
            return {**raced, "created": False}
        raise RuntimeError("could not store the ATS credential in macOS Keychain")
    return {**status, "stored": True, "created": True}
