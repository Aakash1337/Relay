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
    """Keyed digest of an email address (suppression / dedup / log key).

    HMAC-SHA256 under ``RELAY_EMAIL_HASH_PEPPER``: email addresses are
    guessable, so an unkeyed hash of one is reversible by anyone holding
    a candidate address — which defeats the point of storing only a hash
    for DSR-erased do-not-contact entries. The pepper is a long-lived
    secret managed alongside the master key (KMS in production); unlike
    the master key it must NOT rotate casually — every stored digest
    depends on it.
    """
    from relay.config import get_settings  # deferred: avoid import cycle

    pepper = get_settings().email_hash_pepper.get_secret_value()
    return hmac.new(
        pepper.encode("utf-8"),
        canonical_email(email).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def legacy_hash_email(email: str) -> str:
    """The pre-pepper digest (unkeyed SHA-256) — dual-lookup only.

    Rows written before the pepper landed carry this digest. It is never
    written anymore; it is only computed to MATCH old rows while
    ``RELAY_EMAIL_HASH_LEGACY_LOOKUP`` keeps the transition window open.
    """
    return sha256_hex(canonical_email(email))


def email_hash_candidates(email: str) -> tuple[str, ...]:
    """Every digest this address may be stored under, peppered first.

    Membership checks against long-lived hash columns (suppression,
    lead/job matching, DSR erasure) must test all candidates during the
    dual-lookup transition; new writes always use ``hash_email``. Once no
    pre-pepper digests remain, set RELAY_EMAIL_HASH_LEGACY_LOOKUP=false
    and this collapses to the peppered digest alone.
    """
    from relay.config import get_settings  # deferred: avoid import cycle

    peppered = hash_email(email)
    if get_settings().email_hash_legacy_lookup:
        return (peppered, legacy_hash_email(email))
    return (peppered,)


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
