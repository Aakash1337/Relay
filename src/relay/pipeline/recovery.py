"""Crash recovery (Phase 2) — clean up what a dead process left behind.

Because every pipeline step and every send-job claim runs in its own
transaction, a crash can leave exactly two kinds of orphans:

1. a ``pipeline_runs`` row stuck in ``running`` — the harness never got
   to write its outcome. The lead itself is CONSISTENT (its last step
   either fully committed or fully rolled back) and simply resumes on
   the next runner invocation; only the run record needs closing.
2. a ``send_jobs`` row stuck in ``sending`` — the claim committed but
   the completion transaction never landed. The send may or may not
   have physically happened, so the ONLY safe move is to mark the job
   failed and park the lead in ``error_terminal`` for a human: retrying
   automatically could double-send, and pretending it sent could record
   outreach that never happened.

The recovery pass is idempotent and safe to run on every worker tick.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text

from relay import audit
from relay.config import get_settings
from relay.db.engine import tenant_session, untenanted_app_session
from relay.db.models import Lead, PipelineRun, SendJob
from relay.domain.state_machine import transition
from relay.domain.states import LeadState
from relay.logs import get_logger

log = get_logger(__name__)

ACTOR = "worker:recovery"


@dataclass
class RecoveryStats:
    runs_closed: int = 0
    jobs_failed: int = 0
    tenants: list[str] = field(default_factory=list)


def _stale_cutoff(stale_after_seconds: float | None) -> tuple[float, datetime]:
    seconds = (
        stale_after_seconds
        if stale_after_seconds is not None
        else get_settings().recovery_stale_after_seconds
    )
    return seconds, datetime.now(tz=UTC) - timedelta(seconds=seconds)


def recover_orphans(*, stale_after_seconds: float | None = None) -> RecoveryStats:
    """One recovery pass across all tenants. Idempotent."""
    seconds, cutoff = _stale_cutoff(stale_after_seconds)
    stats = RecoveryStats()

    with untenanted_app_session() as session:
        tenant_ids = list(
            session.execute(
                text("SELECT fn_tenants_with_stale_work(:s)"), {"s": seconds}
            ).scalars()
        )

    for tenant_id in tenant_ids:
        stats.tenants.append(str(tenant_id))
        _recover_tenant(tenant_id, cutoff, stats)

    if stats.runs_closed or stats.jobs_failed:
        log.info(
            "crash recovery pass complete",
            runs_closed=stats.runs_closed,
            jobs_failed=stats.jobs_failed,
            tenants=len(stats.tenants),
        )
    return stats


def _recover_tenant(
    tenant_id: uuid.UUID, cutoff: datetime, stats: RecoveryStats
) -> None:
    # 1. Close orphaned run records. The lead state is already consistent.
    with tenant_session(tenant_id) as session:
        stale_runs = (
            session.execute(
                select(PipelineRun).where(
                    PipelineRun.status == "running",
                    PipelineRun.started_at < cutoff,
                )
            )
            .scalars()
            .all()
        )
        for run in stale_runs:
            run.status = "failed"
            run.detail = "orphaned by crash; closed by recovery"
            run.finished_at = datetime.now(tz=UTC)
            audit.record(
                session,
                tenant_id=tenant_id,
                actor_type="worker",
                actor_id=ACTOR,
                action="run.recovered",
                entity_type="pipeline_run",
                entity_id=str(run.id),
                payload={"lead_id": str(run.lead_id) if run.lead_id else None},
            )
            stats.runs_closed += 1

    # 2. Fail orphaned mid-send jobs — never retry, never assume sent.
    with tenant_session(tenant_id) as session:
        stale_jobs = (
            session.execute(
                select(SendJob).where(
                    SendJob.status == "sending",
                    SendJob.started_at < cutoff,
                )
            )
            .scalars()
            .all()
        )
        for job in stale_jobs:
            job.status = "failed"
            job.error = "orphaned mid-send by crash; outcome unknown"
            job.completed_at = datetime.now(tz=UTC)
            lead = session.get(Lead, job.lead_id)
            if lead is not None and lead.state == str(LeadState.SEND_QUEUED):
                transition(
                    session,
                    lead,
                    LeadState.ERROR_TERMINAL,
                    actor=ACTOR,
                    reason="send orphaned by crash; outcome unknown — "
                    "needs human attention",
                )
            audit.record(
                session,
                tenant_id=tenant_id,
                actor_type="worker",
                actor_id=ACTOR,
                action="send.recovered_as_failed",
                entity_type="send_job",
                entity_id=str(job.id),
                payload={"lead_id": str(job.lead_id)},
            )
            stats.jobs_failed += 1
