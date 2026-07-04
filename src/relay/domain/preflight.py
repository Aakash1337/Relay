"""The Legal/Data Preflight gate (Phase 1B) — recorded approval, not vibes.

The artifact (jurisdiction matrix, lawful-basis model, controller/processor
role, provenance rules, privacy notice, retention policy, DSR workflow,
allowed-source list — see docs/legal-data-preflight.md) is authored and
signed off by humans. This module only records that fact, pinned to the
artifact's SHA-256, and answers "is the gate open?". The enforcement
itself lives in ``fn_lead_insert_guard``: without an unrevoked row here,
a real-lawful-basis lead cannot exist, no matter which code path tries.

Approval and revocation are ADMIN acts (the schema-owning role); the
application role can only read the record.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from relay import audit
from relay.db.engine import admin_session
from relay.db.models import DataPreflight
from relay.logs import get_logger

log = get_logger(__name__)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PreflightError(Exception):
    pass


def approve(
    tenant_id: uuid.UUID,
    *,
    artifact_sha256: str,
    approved_by: str,
    artifact_ref: str | None = None,
    notes: str | None = None,
) -> None:
    """Record (or re-record) the tenant's preflight approval.

    Re-approving replaces the record — e.g. after the artifact was
    revised — and clears any revocation. The audit trail keeps history.
    """
    artifact_sha256 = artifact_sha256.strip().lower()
    if not _SHA256_RE.fullmatch(artifact_sha256):
        raise PreflightError("artifact_sha256 must be 64 lowercase hex chars")

    with admin_session() as session:
        record = session.get(DataPreflight, tenant_id)
        if record is None:
            record = DataPreflight(tenant_id=tenant_id)
            session.add(record)
        record.artifact_sha256 = artifact_sha256
        record.artifact_ref = artifact_ref
        record.approved_by = approved_by
        record.approved_at = datetime.now(tz=UTC)
        record.notes = notes
        record.revoked_at = None
        record.revoked_by = None
        audit.record(
            session,
            tenant_id=tenant_id,
            actor_type="human",
            actor_id=approved_by,
            action="preflight.approve",
            entity_type="data_preflight",
            entity_id=str(tenant_id),
            payload={
                "artifact_sha256": artifact_sha256,
                "artifact_ref": artifact_ref,
            },
        )
    log.info(
        "legal/data preflight approved",
        tenant_id=str(tenant_id),
        artifact_sha256=artifact_sha256,
        approved_by=approved_by,
    )


def revoke(tenant_id: uuid.UUID, *, revoked_by: str, reason: str) -> None:
    """Close the gate. Existing rows are untouched (deletion is the DSR /
    retention machinery's job); new real-data ingestion stops immediately."""
    with admin_session() as session:
        record = session.get(DataPreflight, tenant_id)
        if record is None:
            raise PreflightError("no preflight record to revoke")
        record.revoked_at = datetime.now(tz=UTC)
        record.revoked_by = revoked_by
        audit.record(
            session,
            tenant_id=tenant_id,
            actor_type="human",
            actor_id=revoked_by,
            action="preflight.revoke",
            entity_type="data_preflight",
            entity_id=str(tenant_id),
            payload={"reason": reason},
        )
    log.info(
        "legal/data preflight revoked",
        tenant_id=str(tenant_id),
        revoked_by=revoked_by,
    )


def get_record(tenant_id: uuid.UUID) -> DataPreflight | None:
    with admin_session() as session:
        record = session.get(DataPreflight, tenant_id)
        if record is not None:
            session.expunge(record)
        return record


def is_open(tenant_id: uuid.UUID) -> bool:
    record = get_record(tenant_id)
    return record is not None and record.revoked_at is None
