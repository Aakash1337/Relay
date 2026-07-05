"""The hard invariant (§10): a suppressed recipient can never enter
send_eligible — regardless of planner output, campaign state, or human
approval."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from relay.db.engine import tenant_session
from relay.db.models import Lead, SendJob, Suppression
from relay.domain.state_machine import transition
from relay.domain.states import LeadState
from relay.domain.suppression import add_suppression, is_suppressed
from relay.hashing import hash_email
from relay.pipeline.runner import PipelineRunner
from relay.workers.send_worker import process_pending
from tests.conftest import (
    approve_current_draft,
    run_to_approval,
    walk_to_sent,
)

pytestmark = pytest.mark.exit_gate


def test_suppressed_lead_is_blocked_despite_human_approval(tenant_a, factory_a):
    """Approval passes, suppression arrives, eligibility gate blocks."""
    tenant_id, _ = tenant_a
    email = "suppressed-person@example.test"
    lead_id = factory_a.lead(email=email)
    run_to_approval(tenant_id, lead_id)
    approve_current_draft(tenant_id, lead_id)  # human said yes…

    with tenant_session(tenant_id) as session:
        add_suppression(
            session,
            tenant_id=tenant_id,
            email=email,
            reason="do_not_contact",
            source="manual",
            created_by="test",
        )

    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()

    assert outcome.final_state == "send_blocked"
    with tenant_session(tenant_id) as session:
        assert session.execute(select(SendJob)).scalars().all() == [], (
            "no send job may exist for a suppressed recipient"
        )


def test_db_trigger_blocks_send_queued_transition_for_suppressed(tenant_a, factory_a):
    """Skip the runner entirely: hand-drive the transition. The DB
    trigger still refuses."""
    tenant_id, _ = tenant_a
    email = "trigger-test@example.test"
    lead_id = factory_a.lead(email=email)
    run_to_approval(tenant_id, lead_id)
    approve_current_draft(tenant_id, lead_id)

    with tenant_session(tenant_id) as session:
        add_suppression(
            session,
            tenant_id=tenant_id,
            email=email,
            reason="manual",
            source="manual",
            created_by="test",
        )

    with pytest.raises(IntegrityError, match="suppressed"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            lead = session.get(Lead, lead_id)
            assert lead is not None
            transition(
                session,
                lead,
                LeadState.SEND_ELIGIBILITY_PENDING,
                actor="test",
            )
            transition(session, lead, LeadState.SEND_QUEUED, actor="test")


def test_suppression_added_after_queueing_blocks_at_execution(tenant_a, factory_a):
    """The §10 execution-time re-check: suppression arriving between
    queueing and sending stops the send."""
    tenant_id, _ = tenant_a
    email = "late-suppression@example.test"
    lead_id = factory_a.lead(email=email)
    run_to_approval(tenant_id, lead_id)
    approve_current_draft(tenant_id, lead_id)
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.stopped_on == "waiting_worker"  # queued, not sent

    with tenant_session(tenant_id) as session:
        add_suppression(
            session,
            tenant_id=tenant_id,
            email=email,
            reason="unsubscribe",
            source="manual",
            created_by="test",
        )

    stats = process_pending()

    assert stats.sent == 0
    assert stats.blocked == 1
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        job = session.execute(select(SendJob)).scalar_one()
        assert lead is not None and lead.state == "send_blocked"
        assert job.status == "blocked"
        assert "not_suppressed" in (job.error or "")


def test_unsubscribe_auto_creates_suppression_entry(tenant_a, factory_a):
    """Entering 'unsubscribed' creates the suppression row in the same
    transaction (DB trigger), and it permanently blocks re-contact."""
    tenant_id, _ = tenant_a
    email = "unsubscriber@example.test"
    lead_id = factory_a.lead(email=email)
    walk_to_sent(tenant_id, lead_id)

    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None
        transition(session, lead, LeadState.REPLY_RECEIVED, actor="test")
        transition(session, lead, LeadState.TRIAGE_PENDING, actor="test")
        transition(session, lead, LeadState.UNSUBSCRIBED, actor="test")

    with tenant_session(tenant_id) as session:
        entry = session.execute(select(Suppression)).scalar_one()
        assert entry.reason == "unsubscribe"
        assert entry.email_hash == hash_email(email)
        assert entry.applies_to_sales is True

        # A new lead with the same address, new campaign: never eligible.
        assert is_suppressed(session, tenant_id=tenant_id, email_hash=hash_email(email))


def test_hard_bounce_auto_suppresses(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    email = "bouncer@example.test"
    lead_id = factory_a.lead(email=email)
    walk_to_sent(tenant_id, lead_id)

    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None
        transition(session, lead, LeadState.BOUNCE_RECEIVED, actor="test")

    with tenant_session(tenant_id) as session:
        entry = session.execute(select(Suppression)).scalar_one()
        assert entry.reason == "hard_bounce"


def test_suppression_scopes(tenant_a, tenant_b, factory_a):
    """Domain scope, campaign scope, and cross-tenant global scope."""
    tenant_id, _ = tenant_a
    email = "scoped@example.test"

    with tenant_session(tenant_id) as session:
        # Domain-wide suppression for this tenant.
        add_suppression(
            session,
            tenant_id=tenant_id,
            scope="domain",
            domain="blocked-corp.test",
            reason="do_not_contact",
            source="manual",
            created_by="test",
        )
        assert is_suppressed(
            session,
            tenant_id=tenant_id,
            email_hash=hash_email("anyone@blocked-corp.test"),
            domain="blocked-corp.test",
        )
        assert not is_suppressed(
            session,
            tenant_id=tenant_id,
            email_hash=hash_email("someone@other-corp.test"),
            domain="other-corp.test",
        )

    # Global scope is a PLATFORM decision (§17, decided): the app role
    # cannot create one — a tenant must not silently veto every other
    # tenant's sending. RLS rejects the insert.
    with pytest.raises(Exception, match="row-level security"):  # noqa: PT011, SIM117
        with tenant_session(tenant_id) as session:
            add_suppression(
                session,
                tenant_id=tenant_id,
                scope="global",
                email=email,
                reason="legal_delete",
                source="manual",
                created_by="test",
            )

    # Created by the ADMIN path, a global entry reaches across tenants
    # (safe direction: over-suppress).
    from relay.db.engine import admin_session

    with admin_session() as session:
        add_suppression(
            session,
            tenant_id=tenant_id,
            scope="global",
            email=email,
            reason="legal_delete",
            source="manual",
            created_by="admin",
        )
    with tenant_session(tenant_b[0]) as session:
        assert is_suppressed(
            session, tenant_id=tenant_b[0], email_hash=hash_email(email)
        )
