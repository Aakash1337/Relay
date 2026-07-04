"""The human gate (§3): approval is a checkpoint, not a send.

``approve_draft`` moves content through the human gate and nothing else.
The send worker — a separate, internal-only component — later re-checks
every invariant before anything executes. Structured review reasons are
captured for the reviewer rubric (Phase 1A expands the taxonomy).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from relay import audit
from relay.db.models import DraftReview, Lead, OutreachDraft
from relay.domain.state_machine import transition
from relay.domain.states import LeadState
from relay.domain.vocab import ReviewDecision, ReviewReason
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


@dataclass(frozen=True)
class ReviewOutcome:
    review_id: uuid.UUID
    decision: ReviewDecision
    #: The draft that is approved after the review (None unless approved).
    active_draft_id: uuid.UUID | None


def review_draft(
    session: Session,
    *,
    draft: OutreachDraft,
    reviewer: str,
    decision: ReviewDecision | str,
    reasons: Sequence[ReviewReason | str] = (),
    notes: str | None = None,
    edited_subject: str | None = None,
    edited_body: str | None = None,
    run_id: uuid.UUID | None = None,
) -> ReviewOutcome:
    """Apply one rubric review to a pending draft (Phase 1A human gate).

    Every path records an append-only DraftReview row first — the paper
    trail exists even if a later step in the same transaction fails, they
    commit or roll back together. ``approved_with_edits`` supersedes the
    reviewed draft with a new version carrying the human's text, and
    approves *that* version — the send path only ever sees content a
    human actually chose.
    """
    decision = ReviewDecision(decision)
    reason_values = [str(ReviewReason(r)) for r in reasons]
    if decision is not ReviewDecision.APPROVED and not reason_values:
        raise ApprovalError(f"{decision} requires at least one rubric reason")
    if decision is ReviewDecision.APPROVED_WITH_EDITS and not (
        edited_subject or edited_body
    ):
        raise ApprovalError("approved_with_edits requires the edited content")
    if draft.status != "pending_approval":
        raise ApprovalError(
            f"draft is {draft.status!r}, only pending_approval can be reviewed"
        )

    review = DraftReview(
        tenant_id=draft.tenant_id,
        draft_id=draft.id,
        lead_id=draft.lead_id,
        reviewer=reviewer,
        decision=str(decision),
        reasons=reason_values,
        notes=notes,
        edited_subject=edited_subject,
        edited_body=edited_body,
    )
    session.add(review)
    session.flush()

    active_draft_id: uuid.UUID | None = None
    if decision is ReviewDecision.APPROVED:
        approve_draft(session, draft=draft, approver=reviewer, run_id=run_id)
        active_draft_id = draft.id
    elif decision is ReviewDecision.REJECTED:
        reject_draft(
            session,
            draft=draft,
            approver=reviewer,
            reason=", ".join(reason_values),
            run_id=run_id,
        )
    else:  # APPROVED_WITH_EDITS
        lead = _load_lead(session, draft)
        if LeadState(lead.state) is not LeadState.APPROVAL_PENDING:
            raise ApprovalError(f"lead is in {lead.state!r}, not approval_pending")
        draft.status = "rejected"
        draft.review_reason = f"superseded by human edit ({', '.join(reason_values)})"
        edited = OutreachDraft(
            tenant_id=draft.tenant_id,
            lead_id=draft.lead_id,
            campaign_id=draft.campaign_id,
            version=draft.version + 1,
            subject=edited_subject or draft.subject,
            body=edited_body or draft.body,
            personalization_sources={
                **(draft.personalization_sources or {}),
                "_human_edit": {"reviewer": reviewer, "of_version": draft.version},
            },
            status="pending_approval",
        )
        session.add(edited)
        session.flush()
        approve_draft(session, draft=edited, approver=reviewer, run_id=run_id)
        active_draft_id = edited.id
        audit.record(
            session,
            tenant_id=draft.tenant_id,
            actor_type="human",
            actor_id=reviewer,
            action="draft.approve_with_edits",
            entity_type="outreach_draft",
            entity_id=str(edited.id),
            payload={
                "supersedes_version": draft.version,
                "version": edited.version,
                "reasons": reason_values,
            },
        )

    log.info(
        "draft reviewed",
        draft_id=str(draft.id),
        decision=str(decision),
        reasons=reason_values,
        reviewer=reviewer,
    )
    return ReviewOutcome(
        review_id=review.id, decision=decision, active_draft_id=active_draft_id
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
