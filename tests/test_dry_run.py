"""Phase 0 exit gate: no code path can send while dry_run is set.

Attacked from every angle: the runner's mode selection, the DB trigger,
the sender layer, the settings flag, and flag immutability."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from relay.config import get_settings
from relay.db.engine import tenant_session
from relay.db.models import Lead, SendJob
from relay.pipeline.runner import PipelineRunner
from relay.senders import (
    RealSender,
    RealSendUnavailable,
    SimulatedSender,
    sender_for_mode,
)
from relay.workers.send_worker import process_pending
from tests.conftest import approve_current_draft, run_to_approval

pytestmark = pytest.mark.exit_gate


def _queue_lead(tenant_id, factory) -> tuple[uuid.UUID, SendJob]:
    lead_id = factory.lead()
    run_to_approval(tenant_id, lead_id)
    approve_current_draft(tenant_id, lead_id)
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.stopped_on == "waiting_worker"
    with tenant_session(tenant_id) as session:
        job = session.execute(
            select(SendJob).where(SendJob.lead_id == lead_id)
        ).scalar_one()
        session.expunge(job)
    return lead_id, job


def test_dry_run_lead_gets_simulated_job_and_simulated_send(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id, job = _queue_lead(tenant_id, factory_a)
    assert job.mode == "simulated"

    stats = process_pending()
    assert stats.sent == 1

    with tenant_session(tenant_id) as session:
        sent_job = session.execute(
            select(SendJob).where(SendJob.lead_id == lead_id)
        ).scalar_one()
        assert sent_job.status == "sent"
        assert sent_job.provider_message_id is not None
        assert sent_job.provider_message_id.startswith("simulated-")


def test_db_trigger_rejects_real_job_for_dry_run_lead(tenant_a, factory_a):
    """Bypass all Python: INSERT a mode='real' job directly. The
    database refuses."""
    tenant_id, _ = tenant_a
    _, job = _queue_lead(tenant_id, factory_a)
    # Remove the active job so the partial unique index is not what fails.
    with tenant_session(tenant_id) as session:
        active = session.execute(
            select(SendJob).where(SendJob.id == job.id)
        ).scalar_one()
        active.status = "blocked"
        active.error = "test setup"

    with pytest.raises(IntegrityError, match="dry-run"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.add(
                SendJob(
                    tenant_id=tenant_id,
                    campaign_id=job.campaign_id,
                    lead_id=job.lead_id,
                    draft_id=job.draft_id,
                    sequence_step=2,
                    message_version=job.message_version,
                    idempotency_key=f"real-attempt-{uuid.uuid4().hex}",
                    mode="real",
                    recipient_email_hash=job.recipient_email_hash,
                    recipient_domain=job.recipient_domain,
                )
            )
            session.flush()


def test_db_trigger_rejects_flipping_job_mode_to_real(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    _, job = _queue_lead(tenant_id, factory_a)

    with pytest.raises(IntegrityError, match="dry-run|immutable"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            queued = session.execute(
                select(SendJob).where(SendJob.id == job.id)
            ).scalar_one()
            queued.mode = "real"
            session.flush()


def test_dry_run_flag_is_immutable_on_leads(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead(dry_run=True)

    with pytest.raises(IntegrityError, match="dry_run is immutable"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            lead = session.get(Lead, lead_id)
            assert lead is not None
            lead.dry_run = False
            session.flush()


def test_real_sender_cannot_even_be_constructed():
    with pytest.raises(RealSendUnavailable, match="Phase 0"):
        RealSender()


def test_sender_for_real_mode_refuses_while_disabled():
    assert get_settings().real_send_enabled is False
    with pytest.raises(RealSendUnavailable, match="disabled"):
        sender_for_mode("real")


def test_sender_for_real_mode_refuses_even_if_flag_flipped(monkeypatch):
    """Even with RELAY_REAL_SEND_ENABLED=true, the Phase 0 sender layer
    has no real implementation to hand back — construction refuses."""
    monkeypatch.setenv("RELAY_REAL_SEND_ENABLED", "true")
    get_settings.cache_clear()
    try:
        with pytest.raises(RealSendUnavailable, match="Phase 0"):
            sender_for_mode("real")
    finally:
        monkeypatch.delenv("RELAY_REAL_SEND_ENABLED")
        get_settings.cache_clear()


def test_simulated_sender_never_touches_network(tenant_a, factory_a):
    """The simulated sender is pure bookkeeping — it produces an id from
    the job alone."""
    tenant_id, _ = tenant_a
    _, job = _queue_lead(tenant_id, factory_a)
    message_id = SimulatedSender().send(job=job, draft=None)  # type: ignore[arg-type]
    assert message_id == f"simulated-{job.id}"


def test_runner_selects_simulated_even_for_non_dry_run_lead(tenant_a, factory_a):
    """A lead with dry_run=False in a dry-run campaign still simulates:
    effective dry-run is the OR of both flags — and real mode would
    additionally need the settings flag plus Phase 1C infrastructure."""
    tenant_id, _ = tenant_a
    campaign_id = factory_a.campaign(dry_run=True, simulated_replies=True)
    lead_id = factory_a.lead(campaign_id=campaign_id, dry_run=False)
    run_to_approval(tenant_id, lead_id)
    approve_current_draft(tenant_id, lead_id)
    PipelineRunner(tenant_id, lead_id=lead_id).run()

    with tenant_session(tenant_id) as session:
        job = session.execute(
            select(SendJob).where(SendJob.lead_id == lead_id)
        ).scalar_one()
        assert job.mode == "simulated"
