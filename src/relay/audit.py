"""Audit trail helper. Every consequential action leaves a redacted record."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from relay.db.models import AuditLog
from relay.logs import redact_payload


def record(
    session: Session,
    *,
    tenant_id: uuid.UUID,
    actor_type: str,
    actor_id: str,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append an audit entry (same transaction as the action it records)."""
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=redact_payload(payload or {}),
        )
    )
