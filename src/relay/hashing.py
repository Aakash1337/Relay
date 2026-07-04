"""Canonical hashing for PII and credentials.

One module so every layer (suppression, logging redaction, API auth) hashes
identically — a suppression entry and a redacted log line for the same
address always share the same digest prefix, keeping logs correlatable
without exposing personal data.
"""

from __future__ import annotations

import hashlib
import hmac


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_email(email: str) -> str:
    """Lowercased, stripped form used for hashing and deduplication."""
    return email.strip().lower()


def hash_email(email: str) -> str:
    """Stable digest of an email address (suppression / dedup / log key)."""
    return sha256_hex(canonical_email(email))


def email_domain(email: str) -> str:
    canonical = canonical_email(email)
    if "@" not in canonical:
        raise ValueError("not an email address")
    return canonical.rsplit("@", 1)[1]


def hash_api_key(api_key: str) -> str:
    """Digest stored for tenant API keys; raw keys are never persisted."""
    return sha256_hex(api_key)


def derive_tenant_key(master_key: str, tenant_id: str, purpose: str) -> bytes:
    """Per-tenant key derivation (HKDF-style, HMAC-SHA256).

    Tenant-scoped encryption keys are a Phase 0 isolation primitive
    (project documentation §3). Phase 0 derives from a dev master key;
    Phase 3 swaps the master key for a KMS-managed one — the derivation
    seam stays the same.
    """
    salt = f"relay:tenant:{tenant_id}".encode()
    info = f"relay:purpose:{purpose}".encode()
    prk = hmac.new(salt, master_key.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()
