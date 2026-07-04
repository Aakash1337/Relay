"""Suppression service (§10 Suppression Contract).

The authoritative check lives in the database (``fn_is_suppressed``,
called by triggers on every send-relevant write); this module is the
Python-side interface to the same function plus entry management.

Open scope decision (documented in the spec, defaulted here): suppression
defaults to per-tenant scope; the 'global' scope is honored across
tenants when present, because over-suppression is the safe direction.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from relay import audit
from relay.db.models import Suppression
from relay.hashing import email_domain, hash_email
from relay.logs import get_logger

log = get_logger(__name__)


def add_suppression(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    reason: str,
    source: str,
    created_by: str,
    email: str | None = None,
    scope: str = "tenant",
    domain: str | None = None,
    mailbox_id: str | None = None,
    campaign_id: uuid.UUID | None = None,
    expires_at: datetime | None = None,
    applies_to_marketing: bool = True,
    applies_to_sales: bool = True,
) -> Suppression:
    """Add a do-not-contact entry. Only the hash of the address is stored."""
    entry = Suppression(
        tenant_id=tenant_id,
        scope=scope,
        email_hash=hash_email(email) if email else None,
        domain=domain or (email_domain(email) if email else None),
        mailbox_id=mailbox_id,
        campaign_id=campaign_id,
        reason=reason,
        source=source,
        created_by=created_by,
        expires_at=expires_at,
        applies_to_marketing=applies_to_marketing,
        applies_to_sales=applies_to_sales,
    )
    session.add(entry)
    audit.record(
        session,
        tenant_id=tenant_id,
        actor_type="system",
        actor_id=created_by,
        action="suppression.add",
        entity_type="suppression",
        payload={
            "scope": scope,
            "reason": reason,
            "source": source,
            "email_hash": entry.email_hash,
            "domain": entry.domain,
        },
    )
    session.flush()
    log.info(
        "suppression added",
        scope=scope,
        reason=reason,
        email_hash=entry.email_hash,
    )
    return entry


def is_suppressed(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    email_hash: str,
    domain: str | None = None,
    campaign_id: uuid.UUID | None = None,
    mailbox_id: str | None = None,
) -> bool:
    """Ask the database — the same function the triggers enforce with."""
    return bool(
        session.execute(
            text(
                "SELECT fn_is_suppressed("
                ":tenant, :email_hash, :domain, :campaign, :mailbox)"
            ),
            {
                "tenant": str(tenant_id),
                "email_hash": email_hash,
                "domain": domain,
                "campaign": str(campaign_id) if campaign_id else None,
                "mailbox": mailbox_id,
            },
        ).scalar()
    )
