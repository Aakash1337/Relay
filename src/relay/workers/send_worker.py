"""The internal-only send worker (§10).

Approval does not send. This worker — not an API endpoint — picks up
queued send jobs and re-checks **every** invariant at execution time:

- the eligibility gate runs again, in full, per job;
- claiming the job (queued → sending) fires the DB trigger, which
  re-checks suppression, dry-run mode, and approval structurally;
- each job is processed in its own transaction under its tenant's RLS
  context (the worker never operates outside a tenant scope), claimed
  with FOR UPDATE SKIP LOCKED so concurrent workers cannot double-send —
  and even if they raced, the idempotency UNIQUE constraint would hold.

Run via CLI (``relay-worker --once``) or the n8n spine's scheduled tick.
"""

from __future__ import annotations

import argparse
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select, text

from relay import audit
from relay.db.engine import tenant_session, untenanted_app_session
from relay.db.models import Campaign, Lead, OutreachDraft, SendJob
from relay.domain import eligibility
from relay.domain.state_machine import transition
from relay.domain.states import LeadState
from relay.logs import get_logger, setup_logging
from relay.senders import RealSendUnavailable, sender_for_mode

log = get_logger(__name__)

ACTOR = "worker:send"


@dataclass
class WorkerStats:
    sent: int = 0
    blocked: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return self.sent + self.blocked + self.failed


def process_pending(max_jobs: int = 100) -> WorkerStats:
    """Process queued send jobs across all tenants, one tenant at a time."""
    stats = WorkerStats()
    with untenanted_app_session() as session:
        tenant_ids = [
            row[0]
            for row in session.execute(
                text("SELECT * FROM fn_tenants_with_queued_jobs()")
            )
        ]
    for tenant_id in tenant_ids:
        while stats.processed < max_jobs:
            if not _process_one(tenant_id, stats):
                break
    log.info(
        "worker pass complete",
        sent=stats.sent,
        blocked=stats.blocked,
        failed=stats.failed,
    )
    return stats


def _claim_next(session) -> SendJob | None:  # noqa: ANN001
    return session.execute(
        select(SendJob)
        .where(SendJob.status == "queued")
        .order_by(SendJob.queued_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()


def _process_one(tenant_id: uuid.UUID, stats: WorkerStats) -> bool:
    """Process at most one job in its own transaction. True if one existed."""
    job_id: uuid.UUID | None = None
    try:
        with tenant_session(tenant_id) as session:
            job = _claim_next(session)
            if job is None:
                return False
            job_id = job.id

            lead = session.get(Lead, job.lead_id)
            campaign = session.get(Campaign, job.campaign_id)
            draft = session.get(OutreachDraft, job.draft_id)
            if lead is None or campaign is None or draft is None:
                raise LookupError("send job references missing rows")

            # Execution-time re-check of the full eligibility gate.
            result = eligibility.evaluate(
                session,
                lead=lead,
                campaign=campaign,
                draft=draft,
                mode=job.mode,
            )
            # The job itself is the idempotency record — its own existence
            # must not fail the gate at execution time.
            blocking = [
                c for c in result.failures if c.name != "idempotency_key_unused"
            ]
            if blocking:
                detail = "; ".join(f"{c.name}: {c.detail}" for c in blocking)
                job.status = "blocked"
                job.error = detail
                job.completed_at = datetime.now(tz=UTC)
                transition(
                    session,
                    lead,
                    LeadState.SEND_BLOCKED,
                    actor=ACTOR,
                    reason=f"execution-time eligibility failure: {detail}",
                )
                audit.record(
                    session,
                    tenant_id=tenant_id,
                    actor_type="worker",
                    actor_id=ACTOR,
                    action="send.blocked",
                    entity_type="send_job",
                    entity_id=str(job.id),
                    payload={"detail": detail},
                )
                stats.blocked += 1
                return True

            # Claim: the DB trigger re-checks suppression/dry-run/approval
            # on this exact status change.
            job.status = "sending"
            job.started_at = datetime.now(tz=UTC)
            session.flush()

            sender = sender_for_mode(job.mode)
            message_id = sender.send(job=job, draft=draft)

            job.status = "sent"
            job.provider_message_id = message_id
            job.completed_at = datetime.now(tz=UTC)
            session.flush()

            transition(
                session,
                lead,
                LeadState.SENT,
                actor=ACTOR,
                reason=f"{job.mode} send executed",
            )
            audit.record(
                session,
                tenant_id=tenant_id,
                actor_type="worker",
                actor_id=ACTOR,
                action="send.executed",
                entity_type="send_job",
                entity_id=str(job.id),
                payload={
                    "mode": job.mode,
                    "provider_message_id": message_id,
                    "message_version": job.message_version,
                },
            )
            stats.sent += 1
            return True
    except RealSendUnavailable as exc:
        _mark_failed(tenant_id, job_id, str(exc))
        stats.failed += 1
        stats.errors.append(str(exc))
        return True
    except Exception as exc:  # noqa: BLE001 — worker must not crash the loop
        log.error(
            "send job processing failed",
            send_job_id=str(job_id) if job_id else None,
            error=str(exc),
        )
        if job_id is not None:
            _mark_failed(tenant_id, job_id, str(exc))
            stats.failed += 1
            stats.errors.append(str(exc))
            return True
        stats.errors.append(str(exc))
        return False


def _mark_failed(tenant_id: uuid.UUID, job_id: uuid.UUID | None, error: str) -> None:
    """Durably record a failure in a fresh transaction (the claim rolled
    back, so the job is queued again); park the lead in error_retryable."""
    if job_id is None:
        return
    with tenant_session(tenant_id) as session:
        job = session.get(SendJob, job_id)
        if job is None or job.status not in ("queued", "sending"):
            return
        job.status = "failed"
        job.error = error[:2000]
        job.completed_at = datetime.now(tz=UTC)
        lead = session.get(Lead, job.lead_id)
        if lead is not None and lead.state == str(LeadState.SEND_QUEUED):
            transition(
                session,
                lead,
                LeadState.ERROR_RETRYABLE,
                actor=ACTOR,
                reason=f"send execution failed: {error[:200]}",
            )
        audit.record(
            session,
            tenant_id=tenant_id,
            actor_type="worker",
            actor_id=ACTOR,
            action="send.failed",
            entity_type="send_job",
            entity_id=str(job_id),
            payload={"error": error[:500]},
        )


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(
        description="RELAY internal send worker (never exposed as an API)"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process pending jobs once and exit (default behavior)",
    )
    parser.add_argument("--max-jobs", type=int, default=100)
    parser.parse_args()
    stats = process_pending()
    log.info(
        "worker finished",
        sent=stats.sent,
        blocked=stats.blocked,
        failed=stats.failed,
    )


if __name__ == "__main__":
    main()
