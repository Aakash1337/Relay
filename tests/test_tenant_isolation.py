"""Phase 0 exit gate: a cross-tenant read/transition is rejected.

Enforced by Postgres (FORCEd RLS + composite FKs + immutable tenant_id),
tested through the application role — not through code-level politeness."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from relay.db.engine import admin_session, tenant_session
from relay.db.models import Campaign, Lead, LeadTransition, SendJob
from relay.domain.state_machine import transition
from relay.domain.states import LeadState

pytestmark = pytest.mark.exit_gate


def test_cross_tenant_read_returns_nothing(tenant_a, tenant_b, factory_a):
    lead_id = factory_a.lead()

    # Tenant A sees its lead.
    with tenant_session(tenant_a[0]) as session:
        assert session.get(Lead, lead_id) is not None

    # Tenant B sees nothing — not an error, an empty world.
    with tenant_session(tenant_b[0]) as session:
        assert session.get(Lead, lead_id) is None
        assert session.execute(select(Lead)).scalars().all() == []


def test_cross_tenant_update_hits_zero_rows(tenant_a, tenant_b, factory_a):
    lead_id = factory_a.lead()

    with tenant_session(tenant_b[0]) as session:
        result = session.execute(
            text("UPDATE leads SET state = 'closed' WHERE id = :id"),
            {"id": str(lead_id)},
        )
        assert result.rowcount == 0  # RLS filtered the row away

    with tenant_session(tenant_a[0]) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None and lead.state == "created"


def test_cross_tenant_transition_is_impossible(tenant_a, tenant_b, factory_a):
    """The exit-gate wording: a cross-tenant transition is rejected."""
    lead_id = factory_a.lead()

    with tenant_session(tenant_b[0]) as session:
        lead = session.get(Lead, lead_id)
        # The lead does not exist in tenant B's world; there is nothing
        # to transition. This IS the structural rejection.
        assert lead is None


def test_insert_for_other_tenant_rejected(tenant_a, tenant_b, factory_a):
    """WITH CHECK: a session pinned to tenant B cannot write tenant A
    rows."""
    campaign_id = factory_a.campaign()
    source_id = factory_a.source()

    with pytest.raises((ProgrammingError, IntegrityError)):  # noqa: SIM117
        with tenant_session(tenant_b[0]) as session:
            session.add(
                Campaign(
                    tenant_id=tenant_a[0],  # forged tenant_id
                    name=f"forged-{uuid.uuid4().hex[:8]}",
                )
            )
            session.flush()

    # And the composite-FK design: tenant B cannot hang a lead off
    # tenant A's campaign/source even if it forges its own tenant_id.
    with pytest.raises((ProgrammingError, IntegrityError)):  # noqa: SIM117
        with tenant_session(tenant_b[0]) as session:
            session.add(
                Lead(
                    tenant_id=tenant_b[0],
                    campaign_id=campaign_id,  # tenant A's campaign
                    source_id=source_id,  # tenant A's source
                    source_terms_status="yes",
                    lawful_basis="synthetic",
                    region_assumption="none-synthetic",
                    email="forged@example.test",
                    email_hash="0" * 64,
                    email_domain="example.test",
                )
            )
            session.flush()


def test_tenant_id_is_immutable_even_for_admin(tenant_a, tenant_b, factory_a):
    lead_id = factory_a.lead()
    with pytest.raises(Exception, match="immutable"):  # noqa: SIM117
        with admin_session() as session:
            session.execute(
                text("UPDATE leads SET tenant_id = :other WHERE id = :id"),
                {"other": str(tenant_b[0]), "id": str(lead_id)},
            )


def test_no_tenant_context_sees_empty_world(tenant_a, factory_a):
    factory_a.lead()
    from relay.db.engine import untenanted_app_session

    with untenanted_app_session() as session:
        assert session.execute(select(Lead)).scalars().all() == []
        assert session.execute(select(Campaign)).scalars().all() == []
        assert session.execute(select(SendJob)).scalars().all() == []
        assert session.execute(select(LeadTransition)).scalars().all() == []


def test_transition_service_under_wrong_tenant_cannot_act(
    tenant_a, tenant_b, factory_a
):
    """Belt and braces: even loading via admin and transitioning inside a
    tenant-B session fails, because RLS hides the UPDATE target."""
    lead_id = factory_a.lead()
    with admin_session() as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None
        session.expunge(lead)

    # The rejection can surface as a zero-row UPDATE (StaleDataError) or
    # as RLS WITH CHECK refusing the trace/audit inserts — either way,
    # nothing commits.
    from sqlalchemy.orm.exc import StaleDataError

    with pytest.raises(  # noqa: SIM117
        (StaleDataError, IntegrityError, ProgrammingError)
    ):
        with tenant_session(tenant_b[0]) as session:
            merged = session.merge(lead, load=False)
            transition(
                session,
                merged,
                LeadState.SOURCE_CHECKED,
                actor="attacker",
            )

    # And tenant A's lead is untouched.
    with tenant_session(tenant_a[0]) as session:
        untouched = session.get(Lead, lead_id)
        assert untouched is not None and untouched.state == "created"
