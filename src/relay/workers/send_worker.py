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
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select, text

from relay import audit
from relay.db.engine import tenant_session, untenanted_app_session
from relay.db.models import Campaign, Lead, OutreachDraft, SendJob, Tenant
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
    deferred: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def processed(self) -> int:
        return self.sent + self.blocked + self.failed

    def merge(self, other: WorkerStats) -> None:
        """Fold another stream's counters into this one (field list lives
        HERE so a new counter cannot be forgotten far from the class)."""
        self.sent += other.sent
        self.blocked += other.blocked
        self.failed += other.failed
        self.deferred += other.deferred
        self.errors.extend(other.errors)


#: Hard ceiling on drain threads: the app engine runs SQLAlchemy's default
#: QueuePool (5 + 10 overflow = 15 connections). Oversubscribing it makes
#: threads block on connection checkout and REDUCES throughput; 8 leaves
#: headroom for the API process sharing the pool.
_MAX_WORKER_CONCURRENCY = 8


def process_pending(max_jobs: int = 100, *, concurrency: int = 1) -> WorkerStats:
    """Process queued send jobs across all tenants.

    ``max_jobs`` is the GLOBAL budget for the whole pass — shared across
    tenant streams via a thread-safe counter, so a tick is bounded the
    same whether one tenant is queued or fifty.

    Every tick starts with a crash-recovery pass: orphaned runs get
    closed and orphaned mid-send jobs failed BEFORE new work is claimed,
    so the system self-heals on its normal schedule with no separate
    recovery deployment to forget.

    ``concurrency`` (Phase 4 scaling): tenants are independent work
    streams — each job runs in its own transaction under its tenant's
    RLS context, claims are FOR UPDATE SKIP LOCKED, and the real-send
    caps serialize per tenant on an advisory lock — so processing
    DIFFERENT tenants in parallel threads changes throughput, not
    semantics. Clamped to ``_MAX_WORKER_CONCURRENCY``.
    """
    from relay.pipeline.recovery import recover_orphans

    recover_orphans()
    with untenanted_app_session() as session:
        tenant_ids = [
            row[0]
            for row in session.execute(
                text("SELECT * FROM fn_tenants_with_queued_jobs()")
            )
        ]

    budget_lock = threading.Lock()
    budget = {"remaining": max_jobs}

    def take_slot() -> bool:
        with budget_lock:
            if budget["remaining"] <= 0:
                return False
            budget["remaining"] -= 1
            return True

    def return_slot() -> None:
        with budget_lock:
            budget["remaining"] += 1

    def drain_tenant(tenant_id: uuid.UUID) -> WorkerStats:
        tenant_stats = WorkerStats()
        # Resolve the tenant's sending identity ONCE per pass (constant
        # within it) instead of per job. Fail CLOSED: without the tenant
        # row we cannot know which identity to send as — skip the whole
        # tenant rather than guess the global one.
        try:
            with tenant_session(tenant_id) as session:
                tenant = session.get(Tenant, tenant_id)
                if tenant is None:
                    raise LookupError("tenant row unavailable under RLS")
                sender_identity = tenant.sender_from_address
        except Exception as exc:  # noqa: BLE001 — one tenant must not kill the pass
            log.error(
                "tenant sender identity unavailable; skipping tenant",
                tenant_id=str(tenant_id),
                error=str(exc),
            )
            tenant_stats.errors.append(f"tenant {tenant_id}: {exc}")
            return tenant_stats
        while take_slot():
            if not _process_one(
                tenant_id, tenant_stats, sender_identity=sender_identity
            ):
                return_slot()  # nothing consumed the slot
                break
        return tenant_stats

    workers = max(1, min(concurrency, len(tenant_ids), _MAX_WORKER_CONCURRENCY))
    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            per_tenant = list(pool.map(drain_tenant, tenant_ids))
    else:
        per_tenant = [drain_tenant(tenant_id) for tenant_id in tenant_ids]
    stats = WorkerStats()
    for tenant_stats in per_tenant:
        stats.merge(tenant_stats)
    log.info(
        "worker pass complete",
        sent=stats.sent,
        blocked=stats.blocked,
        failed=stats.failed,
        deferred=stats.deferred,
        errors=len(stats.errors),
        tenants=len(tenant_ids),
        concurrency=workers,
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


def _load_job_rows(
    session,
    job: SendJob,  # noqa: ANN001
) -> tuple[Lead | None, Campaign | None, OutreachDraft | None]:
    """Fetch a job's lead, campaign, and draft (PK lookups, identity-cached)."""
    return (
        session.get(Lead, job.lead_id),
        session.get(Campaign, job.campaign_id),
        session.get(OutreachDraft, job.draft_id),
    )


def _process_one(
    tenant_id: uuid.UUID,
    stats: WorkerStats,
    *,
    sender_identity: str | None = None,
) -> bool:
    """Process at most one job in its own transaction. True if one existed.

    ``sender_identity`` is the tenant's own from-address (resolved once
    per pass by the caller); None means the provider's global identity.
    """
    job_id: uuid.UUID | None = None
    try:
        with tenant_session(tenant_id) as session:
            job = _claim_next(session)
            if job is None:
                return False
            job_id = job.id

            lead, campaign, draft = _load_job_rows(session, job)
            if lead is None or campaign is None or draft is None:
                raise LookupError("send job references missing rows")

            # Execution-time re-check of the full eligibility gate. The job
            # being executed is excluded from the idempotency check (it is
            # itself the idempotency record).
            result = eligibility.evaluate(
                session,
                lead=lead,
                campaign=campaign,
                draft=draft,
                mode=job.mode,
                exclude_send_job_id=job.id,
                at_execution=True,
            )
            if not result.eligible:
                failure_names = {c.name for c in result.failures}
                if failure_names <= eligibility.DEFERRABLE_CHECKS:
                    # Pacing, not ineligibility: the job stays queued,
                    # untouched, for a later tick. Jobs are FIFO per
                    # tenant, so stop this tenant's pass — everything
                    # behind this job is paced out too.
                    stats.deferred += 1
                    log.info(
                        "send deferred by pacing",
                        send_job_id=str(job.id),
                        failures=sorted(failure_names),
                    )
                    return False
                detail = result.failure_summary()
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
            message_id = sender.send(
                job=job,
                draft=draft,
                lead=lead,
                sender_identity=sender_identity,
            )

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
                    # Which identity the mail actually left under (None =
                    # the provider's global from-address) — a compliance
                    # incident must be able to answer this per job.
                    "sender_identity": sender_identity,
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
    """Durably record a send failure in a fresh transaction.

    The claim transaction rolled back, so the job is 'queued' again here.
    We mark it 'failed' (terminal) and move the lead to error_terminal —
    NOT error_retryable. Automatic send retry would need a re-queue path
    that does not exist in Phase 0 (the idempotency UNIQUE constraint
    blocks a second job for the same version), so promising "retryable"
    would strand the lead in send_queued forever. Honest, resumable send
    retry is a Phase 2 concern; a failed send needs human/ops attention.

    Best-effort: this runs while handling another failure, so it must not
    raise (e.g. if the DB error that caused the original failure persists).
    """
    if job_id is None:
        return
    try:
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
                    LeadState.ERROR_TERMINAL,
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
    except Exception as exc:  # noqa: BLE001 — recovery must not itself crash
        log.error(
            "failed to record send failure",
            send_job_id=str(job_id),
            error=str(exc),
        )


def main() -> None:
    setup_logging()
    # Make AWS creds in a local .env visible to boto3's credential chain
    # (real-mode sends only). A no-op when there is no .env or when the
    # deployment already sets the vars in the environment.
    from relay.bootstrap import load_local_dotenv

    load_local_dotenv()
    parser = argparse.ArgumentParser(
        description="RELAY internal send worker (never exposed as an API)"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process pending jobs once and exit (default behavior)",
    )
    parser.add_argument("--max-jobs", type=int, default=100)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Tenants processed in parallel (per-tenant order is kept)",
    )
    args = parser.parse_args()
    stats = process_pending(max_jobs=args.max_jobs, concurrency=args.concurrency)
    log.info(
        "worker finished",
        sent=stats.sent,
        blocked=stats.blocked,
        failed=stats.failed,
        deferred=stats.deferred,
    )


if __name__ == "__main__":
    main()
