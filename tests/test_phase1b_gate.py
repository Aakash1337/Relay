"""Phase 1B exit gates: the preflight gate, the closed send path, and DSR.

Roadmap exit criteria under test:
- real prospects flow source→score→draft with provenance on every record;
- no send path is reachable for a real person's lead (code AND trigger);
- deletion/DSR removes a record from datastore and CRM while leaving a
  hashed suppression entry.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from relay.config import get_settings
from relay.crm.registry import crm_adapter, reset_crm
from relay.db.engine import tenant_session
from relay.db.models import (
    Lead,
    LeadTransition,
    OutreachDraft,
    Reply,
    SendJob,
    Suppression,
)
from relay.domain import dsr, preflight
from relay.domain.vocab import COMPLIANCE_FREE_BASES, LawfulBasis
from relay.hashing import hash_email
from relay.pipeline.runner import PipelineRunner
from relay.synthetic.generator import ReplyIntent
from relay.synthetic.seed import create_simulated_reply
from tests.conftest import approve_current_draft, run_to_approval, walk_to_sent

pytestmark = pytest.mark.exit_gate

_SHA = "a" * 64
_RETENTION = datetime.now(tz=UTC) + timedelta(days=180)


def _approve_preflight(tenant_id) -> None:
    preflight.approve(
        tenant_id,
        artifact_sha256=_SHA,
        approved_by="compliance-owner",
        artifact_ref="docs/legal-data-preflight.md@test",
    )


def _real_lead(factory, **overrides):
    """A lead asserting a REAL person's data (legitimate interest)."""
    defaults = {
        "email": f"prospect-{uuid.uuid4().hex[:8]}@northwind-corp.com",
        "lawful_basis": "legitimate_interest",
        "region_assumption": "us-b2b",
        "retention_until": _RETENTION,
    }
    defaults.update(overrides)
    return factory.lead(**defaults)


# ── Vocabulary pin: SQL and Python must agree on what "real data" is ────────


def test_compliance_free_bases_pin():
    """fn_lead_insert_guard and fn_send_jobs_guard hardcode this pair in
    SQL. If this test fails, update BOTH functions and this pin together."""
    expected = {LawfulBasis.SYNTHETIC, LawfulBasis.TEST_CONSENT}
    assert expected == COMPLIANCE_FREE_BASES


# ── The preflight gate ───────────────────────────────────────────────────────


def test_real_basis_rejected_without_preflight(tenant_a, factory_a):
    with pytest.raises(IntegrityError, match="Legal/Data Preflight"):
        _real_lead(factory_a)


def test_preflight_opens_gate_and_revocation_closes_it(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    _approve_preflight(tenant_id)
    lead_id = _real_lead(factory_a)
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None
        # Provenance on every record (§7 + preflight).
        assert lead.source_id and lead.source_terms_status == "yes"
        assert lead.lawful_basis == "legitimate_interest"
        assert lead.retention_until is not None

    preflight.revoke(tenant_id, revoked_by="compliance-owner", reason="test")
    with pytest.raises(IntegrityError, match="Legal/Data Preflight"):
        _real_lead(factory_a)


def test_preflight_of_one_tenant_does_not_open_another(tenant_a, tenant_b, factory_b):
    _approve_preflight(tenant_a[0])
    with pytest.raises(IntegrityError, match="Legal/Data Preflight"):
        _real_lead(factory_b)


def test_real_basis_requires_real_domain_and_retention(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    _approve_preflight(tenant_id)
    with pytest.raises(IntegrityError, match="reserved/test domain"):
        _real_lead(factory_a, email=f"p-{uuid.uuid4().hex[:6]}@corp.test")
    with pytest.raises(IntegrityError, match="retention_until"):
        _real_lead(factory_a, retention_until=None)


# ── Real leads: draft yes, send path no ─────────────────────────────────────


def test_real_lead_flows_to_draft_but_send_path_is_closed(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    _approve_preflight(tenant_id)
    lead_id = _real_lead(factory_a)

    # source → verify → score → personalize → human gate: all reachable.
    run_to_approval(tenant_id, lead_id)
    with tenant_session(tenant_id) as session:
        draft = session.execute(
            select(OutreachDraft).where(OutreachDraft.lead_id == lead_id)
        ).scalar_one()
        assert draft.status == "pending_approval"

    # Even a human approval cannot open the send path in Phase 1B.
    approve_current_draft(tenant_id, lead_id)
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.final_state == "send_blocked"
    with tenant_session(tenant_id) as session:
        assert session.execute(select(SendJob)).scalars().all() == []


def test_send_job_for_real_lead_rejected_by_trigger(tenant_a, factory_a):
    """The DB backstop: even raw SQL cannot queue a send for a real
    person's lead, regardless of what the application layer thinks."""
    tenant_id, _ = tenant_a
    _approve_preflight(tenant_id)
    lead_id = _real_lead(factory_a)
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None
        params = {
            "tenant": str(tenant_id),
            "campaign": str(lead.campaign_id),
            "lead": str(lead_id),
            "draft": str(uuid.uuid4()),
            "key": f"raw-{uuid.uuid4().hex}",
            "hash": lead.email_hash,
            "domain": lead.email_domain,
        }
    with pytest.raises(IntegrityError, match="send path is closed"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.execute(
                text(
                    "INSERT INTO send_jobs (tenant_id, campaign_id, lead_id,"
                    " draft_id, sequence_step, message_version,"
                    " idempotency_key, mode, recipient_email_hash,"
                    " recipient_domain) VALUES (:tenant, :campaign, :lead,"
                    " :draft, 1, 1, :key, 'simulated', :hash, :domain)"
                ),
                params,
            )


# ── DSR erasure ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_crm():
    reset_crm()
    yield
    reset_crm()
    get_settings.cache_clear()


def test_erasure_removes_record_and_leaves_hashed_suppression(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    email = f"erase-me-{uuid.uuid4().hex[:8]}@example.test"
    lead_id = factory_a.lead(email=email)
    walk_to_sent(tenant_id, lead_id)
    create_simulated_reply(tenant_id, lead_id, intent=ReplyIntent.INTERESTED)

    result = dsr.execute_erasure(tenant_id, email=email, requested_by="dpo@operator")

    assert result.datastore["leads"] == 1
    assert result.datastore["outreach_drafts"] >= 1
    assert result.datastore["send_jobs"] == 1
    assert result.datastore["replies"] == 1
    assert result.datastore["lead_transitions"] > 0
    assert result.suppression_added

    with tenant_session(tenant_id) as session:
        assert session.get(Lead, lead_id) is None
        for model in (OutreachDraft, SendJob, Reply, LeadTransition):
            rows = (
                session.execute(select(model).where(model.lead_id == lead_id))
                .scalars()
                .all()
            )
            assert rows == [], f"{model.__tablename__} not fully erased"
        # The hashed do-not-contact memory remains — and it bites.
        entry = session.execute(
            select(Suppression).where(Suppression.email_hash == hash_email(email))
        ).scalar_one()
        assert entry.reason == "legal_delete"
        suppressed = session.execute(
            text("SELECT fn_is_suppressed(:t, :h, NULL, NULL, NULL)"),
            {"t": str(tenant_id), "h": hash_email(email)},
        ).scalar()
        assert suppressed is True


def test_erasure_removes_crm_mirror(tenant_a, factory_a, monkeypatch):
    tenant_id, _ = tenant_a
    monkeypatch.setenv("RELAY_CRM_BACKEND", "memory")
    get_settings.cache_clear()
    reset_crm()

    email = f"crm-erase-{uuid.uuid4().hex[:8]}@example.test"
    lead_id = factory_a.lead(email=email)
    PipelineRunner(tenant_id, lead_id=lead_id).run()  # mirrors to CRM
    adapter = crm_adapter()
    assert adapter is not None and str(lead_id) in adapter.leads

    result = dsr.execute_erasure(tenant_id, email=email, requested_by="dpo")
    assert result.crm[str(lead_id)] == "deleted"
    assert str(lead_id) not in adapter.leads


def test_erasure_of_unknown_address_still_suppresses(tenant_a):
    tenant_id, _ = tenant_a
    email = "never-ingested@example.test"
    result = dsr.execute_erasure(tenant_id, email=email, requested_by="dpo")
    assert result.datastore["leads"] == 0
    with tenant_session(tenant_id) as session:
        assert (
            session.execute(
                select(Suppression).where(Suppression.email_hash == hash_email(email))
            ).scalar_one()
            is not None
        )


def test_cross_tenant_erasure_rejected(tenant_a, tenant_b, factory_a):
    tenant_id, _ = tenant_a
    other, _ = tenant_b
    lead_id = factory_a.lead()
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None
        victim_hash = lead.email_hash

    with pytest.raises(  # noqa: SIM117
        (ProgrammingError, Exception), match="cross-tenant|untenanted"
    ):
        with tenant_session(other) as session:
            session.execute(
                text("SELECT fn_dsr_erase(:t, :h)"),
                {"t": str(tenant_id), "h": victim_hash},
            )

    # And the victim's data is still there.
    with tenant_session(tenant_id) as session:
        assert session.get(Lead, lead_id) is not None


# ── Retention purge ──────────────────────────────────────────────────────────


def test_retention_purge_deletes_only_expired(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    expired_email = f"expired-{uuid.uuid4().hex[:6]}@example.test"
    fresh_email = f"fresh-{uuid.uuid4().hex[:6]}@example.test"
    expired_id = factory_a.lead(
        email=expired_email,
        retention_until=datetime.now(tz=UTC) - timedelta(days=1),
    )
    fresh_id = factory_a.lead(
        email=fresh_email,
        retention_until=datetime.now(tz=UTC) + timedelta(days=90),
    )

    purged = dsr.purge_expired(tenant_id)
    assert purged == 1

    with tenant_session(tenant_id) as session:
        assert session.get(Lead, expired_id) is None
        assert session.get(Lead, fresh_id) is not None
        # Retention expiry is NOT an opt-out: no suppression fabricated.
        entries = (
            session.execute(
                select(Suppression).where(
                    Suppression.email_hash == hash_email(expired_email)
                )
            )
            .scalars()
            .all()
        )
        assert entries == []


def test_retention_worker_discovers_tenants(tenant_a, factory_a):
    from relay.workers.retention_worker import run_once

    tenant_id, _ = tenant_a
    factory_a.lead(retention_until=datetime.now(tz=UTC) - timedelta(hours=1))
    stats = run_once()
    assert stats.leads_purged == 1
    assert stats.per_tenant[str(tenant_id)] == 1
