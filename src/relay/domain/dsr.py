"""DSR erasure & retention purge (Phase 1B) — the right to be forgotten.

Order of operations matters and is deliberate:

1. Write the hashed suppression entry FIRST, in the same transaction as
   the datastore deletes. If anything fails, both roll back together —
   there is no window where the person is deleted but re-contactable.
2. Delete every row carrying the person's data via ``fn_dsr_erase`` —
   the app role's only deletion capability, tenant-guarded in SQL.
3. After commit, remove the CRM mirror rows. CRM outcomes are reported
   truthfully in the result; a CRM failure never un-deletes the
   datastore and never silently passes as done.

What remains afterwards: the suppression row (email hash only — the
do-not-contact memory is the point) and audit_log entries (append-only,
PII-redacted before insert; they are the record THAT erasure happened).

Vector store: no vector store exists in Phase 1B; the result says so
explicitly rather than claiming a deletion that had no target. When one
lands (Phase 3), it plugs into ``_erase_external``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text

from relay import audit
from relay.crm.registry import crm_adapter
from relay.db.engine import tenant_session
from relay.db.models import Lead
from relay.domain.suppression import add_suppression
from relay.hashing import email_domain, email_hash_candidates, hash_email
from relay.logs import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class ErasureResult:
    email_hash: str
    #: Rows removed per table (from fn_dsr_erase).
    datastore: dict[str, int]
    #: Lead ids that were erased (as strings).
    lead_ids: list[str] = field(default_factory=list)
    #: 'deleted' | 'no-row' | 'failed: …' | 'disabled' per lead id.
    crm: dict[str, str] = field(default_factory=dict)
    vector_store: str = "not-applicable (no vector store in Phase 1B)"
    suppression_added: bool = False


def _run_erase(session, tenant_id: uuid.UUID, email_hash: str) -> dict[str, Any]:
    row = session.execute(
        text("SELECT fn_dsr_erase(:tenant, :h)"),
        {"tenant": str(tenant_id), "h": email_hash},
    ).scalar_one()
    return dict(row)


def _erase_external(lead_ids: list[str]) -> dict[str, str]:
    """Remove CRM mirror rows; report per-lead outcomes truthfully."""
    adapter = crm_adapter()
    if adapter is None:
        return dict.fromkeys(lead_ids, "disabled (no CRM configured)")
    outcomes: dict[str, str] = {}
    for ref in lead_ids:
        try:
            outcomes[ref] = "deleted" if adapter.delete_lead(ref) else "no-row"
        except Exception as exc:  # noqa: BLE001 — reported, not swallowed
            outcomes[ref] = f"failed: {exc}"
            log.warning(
                "crm erasure failed — operator must retry",
                backend=adapter.name,
                lead_ref=ref,
                error=str(exc),
            )
    return outcomes


def execute_erasure(
    tenant_id: uuid.UUID,
    *,
    email: str,
    requested_by: str,
    suppress: bool = True,
    actor_type: str = "human",
) -> ErasureResult:
    """Erase one person from a tenant's data, by email address.

    ``suppress=True`` (the DSR default) leaves a hashed do-not-contact
    entry. Retention purges pass ``suppress=False``: expiry of a lawful
    basis is not an opt-out and must not fabricate one.
    """
    email_hash = hash_email(email)
    domain = email_domain(email)

    with tenant_session(tenant_id) as session:
        if suppress:
            add_suppression(
                session,
                tenant_id=tenant_id,
                reason="legal_delete",
                source="manual",
                created_by=requested_by,
                actor_type="human",
                email=email,
                scope="tenant",
                domain=domain,
            )
        # Erase under EVERY digest the address may be stored under (pepper
        # dual-lookup): a pre-pepper lead row carries the legacy digest and
        # must not survive an erasure request.
        counts: dict[str, int] = {}
        lead_ids: list[str] = []
        for candidate in email_hash_candidates(email):
            candidate_counts = _run_erase(session, tenant_id, candidate)
            for x in candidate_counts.pop("lead_ids", None) or []:
                if str(x) not in lead_ids:
                    lead_ids.append(str(x))
            for key, value in candidate_counts.items():
                counts[key] = counts.get(key, 0) + int(value)
        audit.record(
            session,
            tenant_id=tenant_id,
            actor_type=actor_type,
            actor_id=requested_by,
            action="dsr.erasure",
            entity_type="lead",
            entity_id=",".join(lead_ids) or None,
            payload={
                "email_hash": email_hash,
                "rows_deleted": counts,
                "suppression_added": suppress,
            },
        )

    crm_outcomes = _erase_external(lead_ids)
    result = ErasureResult(
        email_hash=email_hash,
        datastore={k: int(v) for k, v in counts.items()},
        lead_ids=lead_ids,
        crm=crm_outcomes,
        suppression_added=suppress,
    )
    log.info(
        "dsr erasure executed",
        email_hash=email_hash,
        rows=result.datastore,
        crm=crm_outcomes,
        suppression_added=suppress,
    )
    return result


def purge_expired(tenant_id: uuid.UUID, *, actor: str = "retention-worker") -> int:
    """Delete every lead whose retention deadline has passed.

    Runs the same audited erasure path per address, WITHOUT a
    suppression entry (expiry is not an opt-out). Returns the number of
    leads purged.
    """
    now = datetime.now(tz=UTC)
    with tenant_session(tenant_id) as session:
        expired = session.execute(
            select(Lead.email).where(
                Lead.retention_until.is_not(None), Lead.retention_until <= now
            )
        ).scalars()
        emails = sorted(set(expired))

    purged = 0
    for email in emails:
        result = execute_erasure(
            tenant_id,
            email=email,
            requested_by=actor,
            suppress=False,
            actor_type="worker",
        )
        purged += result.datastore.get("leads", 0)
    if purged:
        log.info("retention purge complete", leads_purged=purged)
    return purged
