"""§7 hard rule: no prospect enters the canonical datastore without a
registered, lawful source and full provenance fields."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from relay.db.engine import tenant_session
from relay.db.models import Lead
from relay.hashing import email_domain, hash_email

pytestmark = pytest.mark.exit_gate


def _lead_kwargs(tenant_id, campaign_id, source_id, **overrides):
    email = f"prospect-{uuid.uuid4().hex[:8]}@example.test"
    kwargs = {
        "tenant_id": tenant_id,
        "campaign_id": campaign_id,
        "source_id": source_id,
        "source_terms_status": "yes",
        "lawful_basis": "synthetic",
        "region_assumption": "none-synthetic",
        "email": email,
        "email_hash": hash_email(email),
        "email_domain": email_domain(email),
    }
    kwargs.update(overrides)
    return kwargs


def test_lead_requires_registered_source(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    campaign_id = factory_a.campaign()
    with pytest.raises(IntegrityError):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.add(Lead(**_lead_kwargs(tenant_id, campaign_id, uuid.uuid4())))
            session.flush()


@pytest.mark.parametrize("terms", ["no", "legal_review_needed"])
def test_lead_from_disallowed_source_rejected(tenant_a, factory_a, terms):
    """A source whose terms are 'no' or unreviewed cannot feed leads —
    live check against the register at insert time."""
    tenant_id, _ = tenant_a
    campaign_id = factory_a.campaign()
    source_id = factory_a.source(terms=terms)
    with pytest.raises(IntegrityError, match="terms"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.add(Lead(**_lead_kwargs(tenant_id, campaign_id, source_id)))
            session.flush()


def test_stale_yes_snapshot_cannot_bypass_register(tenant_a, factory_a):
    """Even claiming source_terms_status='yes' fails if the register
    disagrees."""
    tenant_id, _ = tenant_a
    campaign_id = factory_a.campaign()
    source_id = factory_a.source(terms="no")
    with pytest.raises(IntegrityError):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.add(
                Lead(
                    **_lead_kwargs(
                        tenant_id,
                        campaign_id,
                        source_id,
                        source_terms_status="yes",  # forged snapshot
                    )
                )
            )
            session.flush()


@pytest.mark.parametrize(
    "missing", ["source_terms_status", "lawful_basis", "region_assumption"]
)
def test_provenance_fields_are_not_nullable(tenant_a, factory_a, missing):
    tenant_id, _ = tenant_a
    campaign_id = factory_a.campaign()
    source_id = factory_a.source()
    kwargs = _lead_kwargs(tenant_id, campaign_id, source_id)
    kwargs[missing] = None
    with pytest.raises(IntegrityError):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.add(Lead(**kwargs))
            session.flush()


def test_provenance_is_immutable_after_insert(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    with pytest.raises(IntegrityError, match="immutable"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            lead = session.get(Lead, lead_id)
            assert lead is not None
            lead.lawful_basis = "consent"
            session.flush()


def test_leads_must_be_born_in_created_state(tenant_a, factory_a):
    """No state-machine bypass at insert: leads start at 'created'."""
    tenant_id, _ = tenant_a
    campaign_id = factory_a.campaign()
    source_id = factory_a.source()
    with pytest.raises(IntegrityError, match="created"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.add(
                Lead(
                    **_lead_kwargs(
                        tenant_id,
                        campaign_id,
                        source_id,
                        state="sent",  # skip straight to sent? no.
                    )
                )
            )
            session.flush()


def test_duplicate_lead_in_campaign_rejected(tenant_a, factory_a):
    """Dedup guardrail: same address, same campaign — one lead only."""
    tenant_id, _ = tenant_a
    campaign_id = factory_a.campaign()
    source_id = factory_a.source()
    factory_a.lead(
        campaign_id=campaign_id,
        source_id=source_id,
        email="dupe@example.test",
    )
    with pytest.raises(IntegrityError, match="uq_leads_campaign_email"):
        factory_a.lead(
            campaign_id=campaign_id,
            source_id=source_id,
            email="dupe@example.test",
        )
