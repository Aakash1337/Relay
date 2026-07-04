"""DB-level state machine behavior: illegal transitions, retry caps,
side-condition invariants (§4) — enforced even against hand-written SQL."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from relay.db.engine import tenant_session
from relay.db.models import Lead
from relay.domain.state_machine import (
    TransitionError,
    resume_from_error,
    transition,
)
from relay.domain.states import LeadState
from tests.conftest import walk_to_sent

pytestmark = pytest.mark.exit_gate


def test_illegal_transition_rejected_in_code(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None
        with pytest.raises(TransitionError):
            transition(session, lead, LeadState.SENT, actor="test")


def test_illegal_transition_rejected_by_db_even_via_raw_sql(tenant_a, factory_a):
    """Delete all the Python: raw UPDATE still cannot jump states."""
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    with pytest.raises(IntegrityError, match="illegal lead state"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.execute(
                text("UPDATE leads SET state = 'sent' WHERE id = :id"),
                {"id": str(lead_id)},
            )


def test_send_queued_requires_verification_and_approval_via_raw_sql(
    tenant_a, factory_a
):
    """Even a 'legal' edge into send_queued fails without the §10
    side-conditions (verified email + approved draft)."""
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None
        for state in (
            LeadState.SOURCE_CHECKED,
            LeadState.ENRICHMENT_PENDING,
            LeadState.ENRICHED,
            LeadState.VERIFICATION_PENDING,
            LeadState.VERIFIED,
            LeadState.SCORING_PENDING,
            LeadState.SCORED_QUALIFIED,
            LeadState.PERSONALIZATION_PENDING,
            LeadState.DRAFT_READY,
            LeadState.APPROVAL_PENDING,
            LeadState.APPROVED,
            LeadState.SEND_ELIGIBILITY_PENDING,
        ):
            transition(session, lead, state, actor="test")

    # Legal edge, but no verified email / approved draft: trigger refuses.
    with pytest.raises(IntegrityError):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.execute(
                text("UPDATE leads SET state = 'send_queued' WHERE id = :id"),
                {"id": str(lead_id)},
            )


def test_retry_cap_enforced_by_db(tenant_a, factory_a):
    """A retryable error cannot retry past its cap (§4)."""
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead(max_retries=2)

    for attempt in range(2):  # two error → resume cycles are allowed
        with tenant_session(tenant_id) as session:
            lead = session.get(Lead, lead_id)
            assert lead is not None
            transition(
                session,
                lead,
                LeadState.ERROR_RETRYABLE,
                actor="test",
                reason=f"forced error {attempt}",
            )
        with tenant_session(tenant_id) as session:
            lead = session.get(Lead, lead_id)
            assert lead is not None
            resume_from_error(session, lead, actor="test")

    # Third cycle exceeds max_retries=2 at the resume step.
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None
        transition(session, lead, LeadState.ERROR_RETRYABLE, actor="test")
    with pytest.raises(IntegrityError, match="retry cap"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            lead = session.get(Lead, lead_id)
            assert lead is not None
            resume_from_error(session, lead, actor="test")

    # error_terminal remains reachable — the failure road is never blocked.
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None
        transition(session, lead, LeadState.ERROR_TERMINAL, actor="test")


def test_booked_requires_reply_and_booking_ref(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    walk_to_sent(tenant_id, lead_id)
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None
        transition(session, lead, LeadState.REPLY_RECEIVED, actor="test")
        transition(session, lead, LeadState.TRIAGE_PENDING, actor="test")
        transition(session, lead, LeadState.INTERESTED, actor="test")
        transition(session, lead, LeadState.BOOKING_PENDING, actor="test")

    with pytest.raises(IntegrityError, match="booked"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            lead = session.get(Lead, lead_id)
            assert lead is not None
            lead.booking_ref = None  # no calendar link
            transition(session, lead, LeadState.BOOKED, actor="test")


def test_dry_run_lead_cannot_receive_reply_without_seed_mode(tenant_a, factory_a):
    """§4: reply_received cannot occur for dry-run leads except in
    explicit seed/test mode."""
    tenant_id, _ = tenant_a
    campaign_id = factory_a.campaign(
        dry_run=True,
        simulated_replies=False,  # seed mode OFF
    )
    lead_id = factory_a.lead(campaign_id=campaign_id)
    walk_to_sent(tenant_id, lead_id)

    with pytest.raises(IntegrityError, match="seed/test"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            lead = session.get(Lead, lead_id)
            assert lead is not None
            transition(session, lead, LeadState.REPLY_RECEIVED, actor="test")


def test_unsubscribed_is_terminal_in_db(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    walk_to_sent(tenant_id, lead_id)
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None
        transition(session, lead, LeadState.REPLY_RECEIVED, actor="test")
        transition(session, lead, LeadState.TRIAGE_PENDING, actor="test")
        transition(session, lead, LeadState.UNSUBSCRIBED, actor="test")

    with pytest.raises(IntegrityError, match="illegal"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.execute(
                text("UPDATE leads SET state = 'created' WHERE id = :id"),
                {"id": str(lead_id)},
            )
