"""Multi-step sequences (§17, un-deferred by operator decision).

The design under test: step N+1 re-enters the EXISTING pipeline loop
(sent → personalization_pending) after the campaign's no-reply delay,
drafts a fresh version, and passes the full gauntlet again — its own
human approval, suppression, eligibility, caps. Cancellation is
structural: a reply, bounce, or unsubscribe moves the lead out of
'sent', so the advance can never fire for them.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from relay.db.engine import tenant_session
from relay.db.models import Lead, SendJob
from relay.pipeline.runner import PipelineRunner
from relay.workers.send_worker import process_pending
from tests.conftest import approve_current_draft, walk_to_sent
from tests.test_phase1c_send import pilot_env  # noqa: F401 — fixture reuse

pytestmark = pytest.mark.exit_gate


def _sequence_lead(factory, *, length: int, delay_hours: int = 0) -> uuid.UUID:
    campaign_id = factory.campaign(
        simulated_replies=False,
        sequence_length=length,
        sequence_delay_hours=delay_hours,
    )
    return factory.lead(campaign_id=campaign_id)


def _job_steps(tenant_id, lead_id) -> list[int]:
    with tenant_session(tenant_id) as session:
        return sorted(
            session.execute(
                select(SendJob.sequence_step).where(SendJob.lead_id == lead_id)
            ).scalars()
        )


def test_second_step_drafts_approves_and_sends(tenant_a, factory_a):
    """The full follow-up loop: step 1 sends, the delay (0h) elapses, the
    next tick drafts step 2, a human approves THAT version, and the
    worker sends it as sequence_step=2 — one job per step."""
    tenant_id, _ = tenant_a
    lead_id = _sequence_lead(factory_a, length=2)
    walk_to_sent(tenant_id, lead_id)
    assert _job_steps(tenant_id, lead_id) == [1]

    # Next tick: no reply, delay elapsed → the lead re-enters drafting
    # and stops at the human gate (approval is per step, per §10).
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.stopped_on == "waiting_human", outcome

    approve_current_draft(tenant_id, lead_id)
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.stopped_on == "waiting_worker", outcome
    assert process_pending().sent == 1

    assert _job_steps(tenant_id, lead_id) == [1, 2]
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None and lead.state == "sent"
        versions = dict(
            session.execute(
                select(SendJob.sequence_step, SendJob.message_version).where(
                    SendJob.lead_id == lead_id
                )
            ).all()
        )
        assert versions[2] > versions[1]  # step 2 sent its OWN approved draft


def test_sequence_exhausts_at_configured_length(tenant_a, factory_a):
    """After the last step, further ticks are no-ops — no step 3 for a
    length-2 sequence, ever."""
    tenant_id, _ = tenant_a
    lead_id = _sequence_lead(factory_a, length=2)
    walk_to_sent(tenant_id, lead_id)
    PipelineRunner(tenant_id, lead_id=lead_id).run()
    approve_current_draft(tenant_id, lead_id)
    PipelineRunner(tenant_id, lead_id=lead_id).run()
    process_pending()
    assert _job_steps(tenant_id, lead_id) == [1, 2]

    for _ in range(2):  # further ticks change nothing
        outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
        assert outcome.final_state == "sent"
    assert _job_steps(tenant_id, lead_id) == [1, 2]


def test_delay_gates_the_advance(tenant_a, factory_a):
    """With a 72h no-reply delay, the tick right after step 1 does NOT
    advance — the lead stays in 'sent' with one job."""
    tenant_id, _ = tenant_a
    lead_id = _sequence_lead(factory_a, length=2, delay_hours=72)
    walk_to_sent(tenant_id, lead_id)

    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.final_state == "sent"
    assert _job_steps(tenant_id, lead_id) == [1]


def test_reply_cancels_the_remaining_steps(
    tenant_a,
    factory_a,
    pilot_env,  # noqa: F811 — pytest fixture injected by name
):
    """A reply takes precedence over the advance: the lead goes down the
    triage path and no follow-up is ever drafted. (Real-mode: the DB
    structurally forbids replies for dry-run leads outside seed mode, so
    this scenario only exists for real campaigns.)"""
    from relay.db.models import Reply
    from tests.test_phase1c_send import _PILOT_INBOX

    tenant_id, _ = tenant_a
    campaign_id = factory_a.campaign(
        dry_run=False,
        simulated_replies=False,
        sequence_length=3,
        sequence_delay_hours=0,
    )
    lead_id = factory_a.lead(
        campaign_id=campaign_id,
        dry_run=False,
        lawful_basis="test_consent",
        email=_PILOT_INBOX,
    )
    walk_to_sent(tenant_id, lead_id)
    with tenant_session(tenant_id) as session:
        job_id = session.execute(
            select(SendJob.id).where(SendJob.lead_id == lead_id)
        ).scalar_one()
        # Webhook-shaped: how a real prospect reply lands.
        session.add(
            Reply(
                tenant_id=tenant_id,
                lead_id=lead_id,
                campaign_id=campaign_id,
                send_job_id=job_id,
                simulated=False,
                subject="Re: your note",
                body="Thanks — yes, I'd like to hear more. Let's talk.",
            )
        )

    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    # The reply flow ran (whatever triage decided) — never step 2.
    assert outcome.final_state in {"closed", "not_interested", "unsubscribed"}
    assert _job_steps(tenant_id, lead_id) == [1]


def test_unsubscribe_cancels_the_remaining_steps(tenant_a, factory_a):
    """One-click unsubscribe after step 1: the lead is terminal, further
    ticks are no-ops, no follow-up exists."""
    from relay.ingest.unsubscribe import build_token, process_unsubscribe

    tenant_id, _ = tenant_a
    lead_id = _sequence_lead(factory_a, length=3)
    walk_to_sent(tenant_id, lead_id)
    with tenant_session(tenant_id) as session:
        job_id = session.execute(
            select(SendJob.id).where(SendJob.lead_id == lead_id)
        ).scalar_one()
    assert process_unsubscribe(build_token(tenant_id, lead_id, job_id)) is True

    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.final_state == "unsubscribed"
    assert _job_steps(tenant_id, lead_id) == [1]


def test_suppression_blocks_step_two_at_the_gate(tenant_a, factory_a):
    """An address suppressed AFTER step 1 (complaint, manual DNC) still
    advances to drafting — but step 2 dies at eligibility, exactly like
    any other suppressed send. §10 holds per step."""
    from relay.domain.suppression import add_suppression

    tenant_id, _ = tenant_a
    lead_id = _sequence_lead(factory_a, length=2)
    walk_to_sent(tenant_id, lead_id)
    with tenant_session(tenant_id) as session:
        email = session.get(Lead, lead_id).email
        add_suppression(
            session,
            tenant_id=tenant_id,
            reason="do_not_contact",
            source="manual",
            created_by="test",
            email=email,
        )

    PipelineRunner(tenant_id, lead_id=lead_id).run()  # drafts step 2
    approve_current_draft(tenant_id, lead_id)
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.final_state == "send_blocked"
    assert _job_steps(tenant_id, lead_id) == [1]  # step 2 never queued


def test_step_duplicate_is_still_rejected_by_the_db(tenant_a, factory_a):
    """The natural-key constraint holds per (lead, step, version): cloning
    the step-1 job as step 2 with the same version is fine to ATTEMPT
    only via raw SQL — and re-cloning step 1 itself is rejected."""
    from sqlalchemy.exc import IntegrityError

    tenant_id, _ = tenant_a
    lead_id = _sequence_lead(factory_a, length=1)
    walk_to_sent(tenant_id, lead_id)

    with pytest.raises(IntegrityError):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.execute(
                text(
                    "INSERT INTO send_jobs (tenant_id, campaign_id, lead_id,"
                    " draft_id, sequence_step, message_version,"
                    " idempotency_key, mode, recipient_email_hash,"
                    " recipient_domain) SELECT tenant_id, campaign_id,"
                    " lead_id, draft_id, sequence_step, message_version,"
                    " idempotency_key || '-clone', mode,"
                    " recipient_email_hash, recipient_domain FROM send_jobs"
                    " WHERE lead_id = :lead"
                ),
                {"lead": str(lead_id)},
            )


def test_single_shot_campaigns_are_unchanged(tenant_a, factory_a):
    """Default sequence_length=1: after 'sent' the runner is a no-op for
    non-simulated campaigns — exactly the pre-sequences behavior."""
    tenant_id, _ = tenant_a
    campaign_id = factory_a.campaign(simulated_replies=False)
    lead_id = factory_a.lead(campaign_id=campaign_id)
    walk_to_sent(tenant_id, lead_id)

    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.final_state == "sent"
    assert _job_steps(tenant_id, lead_id) == [1]
