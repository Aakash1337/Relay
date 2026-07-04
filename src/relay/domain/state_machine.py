"""Transition service — the only Python path that changes a lead's state.

Legality is checked here AND re-checked by the database trigger; the trace
row and audit entry land in the same transaction as the state change, so a
transition either fully happens (with its trace) or not at all.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from relay import audit
from relay.db.models import Lead, LeadTransition
from relay.domain.states import LeadState, is_transition_allowed
from relay.logs import get_logger

log = get_logger(__name__)


class TransitionError(Exception):
    """A transition the state machine does not allow."""


def transition(
    session: Session,
    lead: Lead,
    to_state: LeadState,
    *,
    actor: str,
    reason: str | None = None,
    run_id: uuid.UUID | None = None,
) -> bool:
    """Move *lead* to *to_state*. Returns False for an idempotent no-op.

    Raises TransitionError for an illegal move (and even if this check
    were deleted, the DB trigger would reject the UPDATE).
    """
    from_state = LeadState(lead.state)
    if from_state == to_state:
        log.info(
            "transition noop",
            lead_id=str(lead.id),
            state=str(from_state),
        )
        return False

    if not is_transition_allowed(from_state, to_state):
        raise TransitionError(f"illegal transition {from_state} -> {to_state}")

    # Record where a retryable error should resume to.
    if to_state is LeadState.ERROR_RETRYABLE:
        lead.error_return_state = str(from_state)

    lead.state = str(to_state)
    session.add(
        LeadTransition(
            tenant_id=lead.tenant_id,
            lead_id=lead.id,
            from_state=str(from_state),
            to_state=str(to_state),
            actor=actor,
            reason=reason,
            run_id=run_id,
        )
    )
    audit.record(
        session,
        tenant_id=lead.tenant_id,
        actor_type="system" if not actor.startswith("human:") else "human",
        actor_id=actor,
        action="lead.transition",
        entity_type="lead",
        entity_id=str(lead.id),
        payload={
            "from_state": str(from_state),
            "to_state": str(to_state),
            "reason": reason,
            "run_id": str(run_id) if run_id else None,
        },
    )
    # Surface DB-trigger rejections (suppression, approval, dry-run…) now,
    # inside this transaction, not at commit time.
    session.flush()
    log.info(
        "transition",
        lead_id=str(lead.id),
        from_state=str(from_state),
        to_state=str(to_state),
        actor=actor,
    )
    return True


def resume_from_error(
    session: Session,
    lead: Lead,
    *,
    actor: str,
    run_id: uuid.UUID | None = None,
) -> bool:
    """Resume a lead in error_retryable to the state it errored from.

    The DB trigger increments retry_count and enforces the cap.
    """
    if LeadState(lead.state) is not LeadState.ERROR_RETRYABLE:
        raise TransitionError("lead is not in error_retryable")
    if not lead.error_return_state:
        raise TransitionError("lead has no recorded resume state")
    return transition(
        session,
        lead,
        LeadState(lead.error_return_state),
        actor=actor,
        reason="resume from retryable error",
        run_id=run_id,
    )
