"""The human gate (§3): approval is a checkpoint, not a send.

``approve_draft`` moves content through the human gate and nothing else.
The send worker — a separate, internal-only component — later re-checks
every invariant before anything executes. Structured review reasons are
captured for the reviewer rubric (Phase 1A expands the taxonomy).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from relay import audit
from relay.db.models import Lead, OutreachDraft
from relay.domain.state_machine import transition
from relay.domain.states import LeadState
from relay.logs import get_logger

log = get_logger(__name__)


class ApprovalError(Exception):
    pass


def _load_lead(session: Session, draft: OutreachDraft) -> Lead:
    lead = session.get(Lead, draft.lead_id)
    if lead is None:
        raise ApprovalError("draft's lead not found")
    return lead


def approve_draft(
    session: Session,
    *,
    draft: OutreachDraft,
    approver: str,
    run_id: uuid.UUID | None = None,
) -> None:
    """Approve the draft content. Does NOT send — nothing here sends."""
    if draft.status != "pending_approval":
        raise ApprovalError(
            f"draft is {draft.status!r}, only pending_approval can be approved"
        )
    lead = _load_lead(session, draft)
    if LeadState(lead.state) is not LeadState.APPROVAL_PENDING:
        raise ApprovalError(f"lead is in {lead.state!r}, not approval_pending")

    draft.status = "approved"
    draft.approved_by = approver
    draft.approved_at = datetime.now(tz=UTC)
    # The send path requires approval of *this exact message version*.
    lead.approved_message_version = draft.version

    transition(
        session,
        lead,
        LeadState.APPROVED,
        actor=f"human:{approver}",
        reason=f"draft v{draft.version} approved",
        run_id=run_id,
    )
    audit.record(
        session,
        tenant_id=draft.tenant_id,
        actor_type="human",
        actor_id=approver,
        action="draft.approve",
        entity_type="outreach_draft",
        entity_id=str(draft.id),
        payload={
            "version": draft.version,
            "sends": False,  # explicit: approval never sends
        },
    )
    log.info(
        "draft approved (does not send)",
        draft_id=str(draft.id),
        version=draft.version,
        approver=approver,
    )


def reject_draft(
    session: Session,
    *,
    draft: OutreachDraft,
    approver: str,
    reason: str,
    run_id: uuid.UUID | None = None,
) -> None:
    """Reject the draft; the lead leaves the pipeline (Phase 0 terminal)."""
    if draft.status != "pending_approval":
        raise ApprovalError(
            f"draft is {draft.status!r}, only pending_approval can be rejected"
        )
    lead = _load_lead(session, draft)
    draft.status = "rejected"
    draft.review_reason = reason
    transition(
        session,
        lead,
        LeadState.REJECTED_BY_HUMAN,
        actor=f"human:{approver}",
        reason=reason,
        run_id=run_id,
    )
    audit.record(
        session,
        tenant_id=draft.tenant_id,
        actor_type="human",
        actor_id=approver,
        action="draft.reject",
        entity_type="outreach_draft",
        entity_id=str(draft.id),
        payload={"version": draft.version, "reason": reason},
    )
