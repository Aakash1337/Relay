"""Synthetic-data layer: deterministic, adversarial, structurally safe."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from relay.db.engine import tenant_session
from relay.db.models import Lead, Reply
from relay.synthetic import (
    EdgeCase,
    ReplyIntent,
    generate_prospects,
)
from relay.synthetic.seed import (
    create_simulated_reply,
    intent_for_lead,
    seed_campaign,
)
from tests.conftest import walk_to_sent

pytestmark = pytest.mark.exit_gate

#: Substrings identifying each of the generator's injection-bio variants.
_HOSTILE_MARKERS = (
    "ignore all previous instructions",
    "system override",
    "new instructions from the operator",
)


def _is_hostile(bio: str | None) -> bool:
    return bio is not None and any(m in bio.lower() for m in _HOSTILE_MARKERS)


# ── Generator ────────────────────────────────────────────────────────────────


def test_generation_is_deterministic():
    assert generate_prospects(30, seed=7) == generate_prospects(30, seed=7)
    assert generate_prospects(30, seed=7) != generate_prospects(30, seed=8)


def test_every_edge_case_appears_in_a_large_batch():
    batch = generate_prospects(60, seed=1)
    seen = {p.edge_case for p in batch}
    assert seen == set(EdgeCase), f"missing edge cases: {set(EdgeCase) - seen}"


def test_every_reply_intent_appears():
    batch = generate_prospects(12, seed=1)
    assert {p.reply_intent for p in batch} == set(ReplyIntent)


def test_all_emails_are_unresolvable_test_domains():
    for p in generate_prospects(100, seed=2):
        assert p.email.rsplit("@", 1)[1].endswith(".test"), p.email


def test_injection_bios_are_present_and_hostile():
    batch = generate_prospects(100, seed=3)
    hostile = [p for p in batch if p.edge_case is EdgeCase.INJECTION_BIO]
    assert hostile, "no injection-bio prospects generated"
    assert all(_is_hostile(p.bio) for p in hostile)


# ── Seeding through the front door ──────────────────────────────────────────


def test_seed_campaign_inserts_leads_through_real_guards(tenant_a):
    tenant_id, _ = tenant_a
    result = seed_campaign(tenant_id, n=15, seed=42)
    assert len(result.lead_ids) + result.skipped_duplicates == 15
    with tenant_session(tenant_id) as session:
        leads = session.execute(select(Lead)).scalars().all()
        assert len(leads) == len(result.lead_ids)
        # Every seeded lead is dry-run, synthetic-basis, state 'created'.
        assert all(lead.dry_run for lead in leads)
        assert all(lead.lawful_basis == "synthetic" for lead in leads)
        assert all(lead.state == "created" for lead in leads)
        # The hostile edge case made it into the datastore (as inert data).
        assert any(_is_hostile(lead.bio) for lead in leads)


# ── Simulated replies ────────────────────────────────────────────────────────


def test_simulated_reply_requires_a_completed_send(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    with pytest.raises(ValueError, match="no completed send"):
        create_simulated_reply(tenant_id, lead_id)


def test_simulated_reply_respects_campaign_opt_out(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    campaign_id = factory_a.campaign(simulated_replies=False)
    lead_id = factory_a.lead(campaign_id=campaign_id)
    walk_to_sent(tenant_id, lead_id)
    with pytest.raises(ValueError, match="simulated replies disabled"):
        create_simulated_reply(tenant_id, lead_id)


def test_simulated_reply_created_for_sent_lead(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    walk_to_sent(tenant_id, lead_id)
    reply_id = create_simulated_reply(
        tenant_id, lead_id, intent=ReplyIntent.UNSUBSCRIBE
    )
    with tenant_session(tenant_id) as session:
        reply = session.get(Reply, reply_id)
        assert reply is not None
        assert reply.simulated is True
        assert "remove me" in reply.body.lower() or "unsubscribe" in (
            reply.body.lower()
        )
        assert reply.triage_category is None  # triage is the pipeline's job


def test_intent_is_stable_per_lead(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None
        assert intent_for_lead(lead) is intent_for_lead(lead)
