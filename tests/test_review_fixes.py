"""Regression tests for issues found in the Phase 0 adversarial review.

Each test pins a specific hardening so it cannot silently regress.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from relay.db.engine import tenant_session
from relay.db.models import Lead, OutreachDraft, SendJob
from relay.domain.state_machine import transition
from relay.domain.states import LeadState
from relay.guardrails.harness import BudgetExceeded, RunHarness
from relay.pipeline.runner import PipelineRunner
from relay.workers.send_worker import _mark_failed, process_pending
from tests.conftest import approve_current_draft, run_to_approval

pytestmark = pytest.mark.exit_gate


def _queue(tenant_id, factory, **lead_kw) -> tuple[uuid.UUID, SendJob]:
    lead_id = factory.lead(**lead_kw)
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


# ── Approved-draft content is frozen (tamper-evidence) ─────────────────────


def test_approved_draft_content_cannot_be_edited(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    run_to_approval(tenant_id, lead_id)
    draft_id = approve_current_draft(tenant_id, lead_id)

    with pytest.raises(IntegrityError, match="approved draft is immutable"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.execute(
                text("UPDATE outreach_drafts SET body = 'TAMPERED' WHERE id = :id"),
                {"id": str(draft_id)},
            )


def test_draft_cannot_be_inserted_already_approved(tenant_a, factory_a):
    """A draft with INSERT rights cannot be born approved — that would mint
    a self-approved draft and bypass the human gate."""
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    # Give the lead a real campaign/draft chain by walking to a draft.
    run_to_approval(tenant_id, lead_id)
    with tenant_session(tenant_id) as session:
        existing = session.execute(select(OutreachDraft)).scalar_one()
        campaign_id = existing.campaign_id

    with pytest.raises(IntegrityError, match="must be inserted as draft"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.add(
                OutreachDraft(
                    tenant_id=tenant_id,
                    lead_id=lead_id,
                    campaign_id=campaign_id,
                    version=99,
                    subject="self-approved",
                    body="never seen by a human",
                    status="approved",  # born approved? no.
                    approved_by="attacker",
                    approved_at=datetime.now(tz=UTC),
                )
            )
            session.flush()


def test_draft_cannot_be_flipped_approved_without_approver(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    run_to_approval(tenant_id, lead_id)
    with tenant_session(tenant_id) as session:
        draft = session.execute(select(OutreachDraft)).scalar_one()
        draft_id = draft.id

    with pytest.raises(IntegrityError, match="requires approved_by"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.execute(
                text("UPDATE outreach_drafts SET status = 'approved' WHERE id = :id"),
                {"id": str(draft_id)},
            )


# ── Send-job recipient must be its lead's own address ──────────────────────


def test_send_job_recipient_must_match_lead(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    _, job = _queue(tenant_id, factory_a)
    # Park the real job so the one-active index is free.
    with tenant_session(tenant_id) as session:
        parked = session.get(SendJob, job.id)
        parked.status = "blocked"
        parked.error = "test setup"

    with pytest.raises(IntegrityError, match="recipient must match"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.add(
                SendJob(
                    tenant_id=tenant_id,
                    campaign_id=job.campaign_id,
                    lead_id=job.lead_id,
                    draft_id=job.draft_id,
                    sequence_step=2,
                    message_version=job.message_version,
                    idempotency_key=f"mismatch-{uuid.uuid4().hex}",
                    mode="simulated",
                    recipient_email_hash="f" * 64,  # not the lead's hash
                    recipient_domain="attacker.test",
                )
            )
            session.flush()


# ── Real-intent leads are blocked, never silently simulated-sent ───────────


def test_real_intent_lead_is_blocked_not_simulated(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    campaign_id = factory_a.campaign(dry_run=False, simulated_replies=False)
    lead_id = factory_a.lead(campaign_id=campaign_id, dry_run=False)
    run_to_approval(tenant_id, lead_id)
    approve_current_draft(tenant_id, lead_id)

    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()

    assert outcome.final_state == "send_blocked"
    with tenant_session(tenant_id) as session:
        assert session.execute(select(SendJob)).scalars().all() == []


# ── Retry-cap inputs are not editable by the code they police ──────────────


def test_max_retries_is_immutable(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    with pytest.raises(IntegrityError, match="max_retries is immutable"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.execute(
                text("UPDATE leads SET max_retries = 999 WHERE id = :id"),
                {"id": str(lead_id)},
            )


def test_retry_count_cannot_be_reset_externally(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    with pytest.raises(IntegrityError, match="retry_count is managed"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.execute(
                text("UPDATE leads SET retry_count = 5, title = 'x' WHERE id = :id"),
                {"id": str(lead_id)},
            )


# ── error_retryable resumes only to its error_return_state ─────────────────


def test_error_retryable_cannot_two_hop_around_pipeline(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    # Walk to 'enriched', then error out (error_return_state='enriched').
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        for st in (
            LeadState.SOURCE_CHECKED,
            LeadState.ENRICHMENT_PENDING,
            LeadState.ENRICHED,
        ):
            transition(session, lead, st, actor="test")
        transition(session, lead, LeadState.ERROR_RETRYABLE, actor="test")

    # error_retryable -> verification_pending is in the generic rule set but
    # is NOT the recorded return state: the trigger must reject it.
    with pytest.raises(IntegrityError, match="error_return_state"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.execute(
                text("UPDATE leads SET state = 'verification_pending' WHERE id = :id"),
                {"id": str(lead_id)},
            )

    # Resuming to the recorded state (enriched) is allowed.
    with tenant_session(tenant_id) as session:
        session.execute(
            text("UPDATE leads SET state = 'enriched' WHERE id = :id"),
            {"id": str(lead_id)},
        )


# ── Cross-tenant suppression probe is rejected ─────────────────────────────


def test_cross_tenant_suppression_probe_rejected(tenant_a, tenant_b, factory_a):
    tenant_id, _ = tenant_a
    other = tenant_b[0]
    with pytest.raises(Exception, match="cross-tenant suppression probe"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.execute(
                text("SELECT fn_is_suppressed(:other, :h, NULL, NULL, NULL)"),
                {"other": str(other), "h": "a" * 64},
            )


# ── A zero budget is honored, not silently replaced by the default ─────────


def test_zero_budget_is_honored(tenant_a):
    tenant_id, _ = tenant_a
    harness = RunHarness(tenant_id=tenant_id, kind="zero_budget", budget_units=0.0)
    assert harness.budget_units == 0.0  # not the 50-unit default
    with pytest.raises(BudgetExceeded):
        harness.spend(0.1, what="anything")
    harness.finalize_kill()


# ── Failed send parks the lead terminally, never stranded in send_queued ───


def test_failed_send_moves_lead_to_error_terminal(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id, job = _queue(tenant_id, factory_a)

    _mark_failed(tenant_id, job.id, "simulated transient failure")

    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        failed = session.get(SendJob, job.id)
        assert lead is not None and lead.state == "error_terminal"
        assert failed is not None and failed.status == "failed"


# ── The worker honors max_jobs ─────────────────────────────────────────────


def test_worker_respects_max_jobs(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    _queue(tenant_id, factory_a)
    _queue(tenant_id, factory_a)  # two independent queued jobs

    stats = process_pending(max_jobs=1)

    assert stats.sent == 1  # stopped after one, not both
    with tenant_session(tenant_id) as session:
        queued_left = (
            session.execute(select(SendJob).where(SendJob.status == "queued"))
            .scalars()
            .all()
        )
        assert len(queued_left) == 1


# ── The definer-bypass RLS policies exist (portability guarantee) ──────────


def test_definer_bypass_policies_exist(tenant_a):
    """The SECURITY DEFINER functions only bypass FORCE RLS on non-superuser
    owners because of these owner-scoped policies; removing them would break
    global suppression, API auth, and the worker on managed Postgres."""
    from relay.db.engine import admin_engine

    with admin_engine().connect() as conn:
        tables = set(
            conn.execute(
                text(
                    "SELECT tablename FROM pg_policies "
                    "WHERE policyname = 'definer_bypass'"
                )
            ).scalars()
        )
    for required in ("suppression", "tenants", "send_jobs", "leads"):
        assert required in tables, f"missing definer_bypass on {required}"
