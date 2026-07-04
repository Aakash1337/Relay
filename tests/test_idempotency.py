"""Phase 0 exit gate: reprocessing the same lead is a no-op; the
idempotency DB constraint rejects a duplicate send."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from relay.db.engine import tenant_session
from relay.db.models import OutreachDraft, SendJob
from relay.domain.approval import ApprovalError, approve_draft
from relay.workers.send_worker import process_pending
from tests.conftest import (
    approve_current_draft,
    run_to_approval,
    walk_to_sent,
)

pytestmark = pytest.mark.exit_gate


def _get_job(tenant_id, lead_id) -> SendJob:
    with tenant_session(tenant_id) as session:
        job = session.execute(
            select(SendJob).where(SendJob.lead_id == lead_id)
        ).scalar_one()
        session.expunge(job)
        return job


def _queue_and_park_job(tenant_id, factory_a) -> tuple[uuid.UUID, SendJob]:
    """Queue a send, then park the job (queued → blocked) so the lead is
    still in send_queued: the deepest layer — the UNIQUE constraint —
    is what a duplicate INSERT must now get through."""
    from relay.pipeline.runner import PipelineRunner

    lead_id = factory_a.lead()
    run_to_approval(tenant_id, lead_id)
    approve_current_draft(tenant_id, lead_id)
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.stopped_on == "waiting_worker"
    job = _get_job(tenant_id, lead_id)
    with tenant_session(tenant_id) as session:
        parked = session.get(SendJob, job.id)
        assert parked is not None
        parked.status = "blocked"
        parked.error = "test: parked to expose the unique constraint"
    return lead_id, job


def test_duplicate_send_job_rejected_by_db_constraint(tenant_a, factory_a):
    """Even if every code-level check were deleted or raced past, the
    UNIQUE constraint (tenant, campaign, lead, step, version) holds."""
    tenant_id, _ = tenant_a
    _, job = _queue_and_park_job(tenant_id, factory_a)

    with pytest.raises(IntegrityError, match="uq_send_jobs"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.add(
                SendJob(
                    tenant_id=tenant_id,
                    campaign_id=job.campaign_id,
                    lead_id=job.lead_id,
                    draft_id=job.draft_id,
                    sequence_step=job.sequence_step,
                    message_version=job.message_version,
                    idempotency_key=f"replay-{uuid.uuid4().hex}",
                    mode="simulated",
                    recipient_email_hash=job.recipient_email_hash,
                    recipient_domain=job.recipient_domain,
                )
            )
            session.flush()


def test_duplicate_idempotency_key_rejected(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    _, job = _queue_and_park_job(tenant_id, factory_a)

    with pytest.raises(IntegrityError, match="idempotency"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.add(
                SendJob(
                    tenant_id=tenant_id,
                    campaign_id=job.campaign_id,
                    lead_id=job.lead_id,
                    draft_id=job.draft_id,
                    sequence_step=99,  # different natural key…
                    message_version=job.message_version,
                    idempotency_key=job.idempotency_key,  # …same key
                    mode="simulated",
                    recipient_email_hash=job.recipient_email_hash,
                    recipient_domain=job.recipient_domain,
                )
            )
            session.flush()


def test_replayed_job_insert_after_send_rejected(tenant_a, factory_a):
    """After the send completed, a replayed insert is refused by an even
    earlier layer (the lead is no longer in send_queued)."""
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    walk_to_sent(tenant_id, lead_id)
    job = _get_job(tenant_id, lead_id)

    with pytest.raises(IntegrityError):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.add(
                SendJob(
                    tenant_id=tenant_id,
                    campaign_id=job.campaign_id,
                    lead_id=job.lead_id,
                    draft_id=job.draft_id,
                    sequence_step=job.sequence_step,
                    message_version=job.message_version,
                    idempotency_key=f"replay-{uuid.uuid4().hex}",
                    mode="simulated",
                    recipient_email_hash=job.recipient_email_hash,
                    recipient_domain=job.recipient_domain,
                )
            )
            session.flush()


def test_worker_rerun_after_send_is_noop(tenant_a, factory_a):
    """A replayed worker tick (the 'replayed webhook' analogue) finds
    nothing to do — the job is no longer queued."""
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    walk_to_sent(tenant_id, lead_id)

    stats = process_pending()

    assert stats.sent == 0
    assert stats.blocked == 0
    assert stats.failed == 0
    job = _get_job(tenant_id, lead_id)
    assert job.status == "sent"


def test_double_approval_rejected(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    run_to_approval(tenant_id, lead_id)
    draft_id = approve_current_draft(tenant_id, lead_id)

    with pytest.raises(ApprovalError):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            draft = session.get(OutreachDraft, draft_id)
            assert draft is not None
            approve_draft(session, draft=draft, approver="second-approver")


def test_one_active_send_per_lead(tenant_a, factory_a):
    """A lead cannot be in two active campaign send states at once (§4):
    the partial unique index rejects a second queued job."""
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    run_to_approval(tenant_id, lead_id)
    approve_current_draft(tenant_id, lead_id)
    from relay.pipeline.runner import PipelineRunner

    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.stopped_on == "waiting_worker"
    job = _get_job(tenant_id, lead_id)  # queued, not yet sent

    with pytest.raises(IntegrityError, match="one_active"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.add(
                SendJob(
                    tenant_id=tenant_id,
                    campaign_id=job.campaign_id,
                    lead_id=job.lead_id,
                    draft_id=job.draft_id,
                    sequence_step=2,
                    message_version=job.message_version,
                    idempotency_key=f"second-{uuid.uuid4().hex}",
                    mode="simulated",
                    recipient_email_hash=job.recipient_email_hash,
                    recipient_domain=job.recipient_domain,
                )
            )
            session.flush()
