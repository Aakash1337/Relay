"""One-way, best-effort lead sync: RELAY → CRM.

Called after pipeline runs and never from inside a step transaction —
a CRM outage degrades the mirror, not the pipeline, and absolutely not
the safety gates. Failures are logged and swallowed here by contract.
"""

from __future__ import annotations

import uuid

from relay.crm.base import CRMLeadSnapshot
from relay.crm.registry import crm_adapter
from relay.db.engine import tenant_session
from relay.db.models import Lead
from relay.logs import get_logger

log = get_logger(__name__)


def sync_lead(tenant_id: uuid.UUID, lead_id: uuid.UUID, *, context: str) -> bool:
    """Mirror one lead's current status. Returns True if a sync happened."""
    adapter = crm_adapter()
    if adapter is None:
        return False
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            return False
        snapshot = CRMLeadSnapshot(
            external_ref=str(lead.id),
            tenant_ref=str(lead.tenant_id),
            email=lead.email,
            state=lead.state,
            first_name=lead.first_name,
            last_name=lead.last_name,
            title=lead.title,
            company=lead.company_name,
            fit_score=float(lead.fit_score) if lead.fit_score is not None else None,
            dry_run=lead.dry_run,
        )
    try:
        adapter.upsert_lead(snapshot)
        adapter.record_event(
            snapshot.external_ref, "state", f"{context}: {snapshot.state}"
        )
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        log.warning(
            "crm sync failed (pipeline unaffected)",
            backend=adapter.name,
            error=str(exc),
            lead_id=str(lead_id),
        )
        return False
