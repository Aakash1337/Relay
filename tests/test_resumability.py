"""Phase 2 exit gates: forced crash recovers cleanly; no lost or
duplicated work; transient failures park-and-resume under the retry cap."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text

from relay.compute.base import ComputeRefused, ComputeUnavailable
from relay.compute.offline import OfflineBackend
from relay.db.engine import admin_engine, tenant_session
from relay.db.models import Lead, OutreachDraft, PipelineRun, SendJob
from relay.guardrails.harness import RunHarness
from relay.pipeline.recovery import recover_orphans
from relay.pipeline.runner import PipelineRunner
from relay.routing.router import TaskType
from tests.conftest import approve_current_draft, run_to_approval

pytestmark = pytest.mark.exit_gate


class _FlakyBackend(OfflineBackend):
    """Offline backend that fails N times on one task type, then works."""

    def __init__(self, fail_task: TaskType, times: int = 1):
        self._fail_task = fail_task
        self._remaining = times
        self.attempts = 0

    def complete(self, request):
        if request.task_type == self._fail_task and self._remaining > 0:
            self._remaining -= 1
            self.attempts += 1
            raise ComputeUnavailable("simulated provider outage")
        return super().complete(request)


class _RefusingBackend(OfflineBackend):
    def complete(self, request):
        if request.task_type == TaskType.OUTREACH_COPY:
            raise ComputeRefused("simulated safety refusal")
        return super().complete(request)


@pytest.fixture
def _flaky(monkeypatch):
    """Install a backend that fails OUTREACH_COPY exactly once."""
    backend = _FlakyBackend(TaskType.OUTREACH_COPY, times=1)
    monkeypatch.setattr("relay.routing.executors.backend_for", lambda tier: backend)
    return backend


# ── Transient failure: park, resume, no duplicated work ────────────────────


def test_transient_failure_parks_then_resumes_without_duplicates(
    tenant_a, factory_a, _flaky
):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()

    # First run: the outage hits during personalization → parked retryable.
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.final_state == "error_retryable"
    assert outcome.stopped_on == "error_retryable"
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None
        assert lead.error_return_state == "personalization_pending"
        # The failed step's transaction rolled back: NO draft exists.
        assert session.execute(select(OutreachDraft)).scalars().all() == []

    # Second run: resume → personalize succeeds → human gate.
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.stopped_on == "waiting_human"
    with tenant_session(tenant_id) as session:
        drafts = session.execute(select(OutreachDraft)).scalars().all()
        # Exactly ONE draft — the crashed attempt left nothing behind.
        assert [d.version for d in drafts] == [1]
        lead = session.get(Lead, lead_id)
        assert lead is not None and lead.retry_count == 1


def test_retry_cap_exhaustion_parks_terminally(tenant_a, factory_a, monkeypatch):
    tenant_id, _ = tenant_a
    backend = _FlakyBackend(TaskType.OUTREACH_COPY, times=99)  # never recovers
    monkeypatch.setattr("relay.routing.executors.backend_for", lambda tier: backend)
    lead_id = factory_a.lead(max_retries=2)

    states = []
    for _ in range(6):  # more runs than the cap can absorb
        outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
        states.append(outcome.final_state)
        if outcome.final_state == "error_terminal":
            break

    assert states[-1] == "error_terminal"
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None
        assert lead.retry_count == 2  # cap respected exactly


def test_refusal_parks_terminally_not_retryable(tenant_a, factory_a, monkeypatch):
    tenant_id, _ = tenant_a
    monkeypatch.setattr(
        "relay.routing.executors.backend_for", lambda tier: _RefusingBackend()
    )
    lead_id = factory_a.lead()
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.final_state == "error_terminal"  # human attention, no retry


# ── Forced crash: orphaned run record ───────────────────────────────────────


def _backdate(table: str, column: str, where: str, params: dict) -> None:
    with admin_engine().begin() as conn:
        conn.execute(
            text(
                f"UPDATE {table} SET {column} = now() - interval '1 hour' WHERE {where}"
            ),
            params,
        )


def test_orphaned_run_is_closed_by_recovery(tenant_a):
    tenant_id, _ = tenant_a
    # Simulate a crash: a harness starts (run row 'running') and dies.
    harness = RunHarness(tenant_id=tenant_id, kind="crash_sim")
    run_id = harness.run_id
    _backdate("pipeline_runs", "started_at", "id = :id", {"id": str(run_id)})

    stats = recover_orphans()
    assert stats.runs_closed == 1

    with tenant_session(tenant_id) as session:
        run = session.get(PipelineRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert "recovery" in (run.detail or "")
        assert run.finished_at is not None

    # Idempotent: a second pass finds nothing.
    assert recover_orphans().runs_closed == 0


def test_fresh_running_run_is_not_touched(tenant_a):
    tenant_id, _ = tenant_a
    harness = RunHarness(tenant_id=tenant_id, kind="alive")
    stats = recover_orphans()
    assert stats.runs_closed == 0
    harness.complete(detail="finished normally")


# ── Forced crash: orphaned mid-send job ─────────────────────────────────────


def test_orphaned_mid_send_job_fails_safe(tenant_a, factory_a):
    """A job stuck 'sending' has an UNKNOWN outcome: recovery must fail it
    and park the lead for a human — never retry, never assume sent."""
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    run_to_approval(tenant_id, lead_id)
    approve_current_draft(tenant_id, lead_id)
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.stopped_on == "waiting_worker"

    # Simulate the crash: claim the job (queued → sending), then "die".
    with tenant_session(tenant_id) as session:
        job = session.execute(select(SendJob)).scalar_one()
        job.status = "sending"
        job.started_at = datetime.now(tz=UTC)
        job_id = job.id
    _backdate("send_jobs", "started_at", "id = :id", {"id": str(job_id)})

    stats = recover_orphans()
    assert stats.jobs_failed == 1

    with tenant_session(tenant_id) as session:
        job = session.get(SendJob, job_id)
        lead = session.get(Lead, lead_id)
        assert job is not None and job.status == "failed"
        assert "outcome unknown" in (job.error or "")
        assert lead is not None and lead.state == "error_terminal"

    # No new send job can be minted for the same version (idempotency
    # UNIQUE) — recovery cannot become a double-send vector.
    assert recover_orphans().jobs_failed == 0


def test_recovery_runs_on_every_worker_tick(tenant_a):
    """The worker tick self-heals: recovery is part of process_pending."""
    tenant_id, _ = tenant_a
    harness = RunHarness(tenant_id=tenant_id, kind="crash_sim")
    _backdate("pipeline_runs", "started_at", "id = :id", {"id": str(harness.run_id)})
    from relay.workers.send_worker import process_pending

    process_pending(max_jobs=1)
    with tenant_session(tenant_id) as session:
        run = session.get(PipelineRun, harness.run_id)
        assert run is not None and run.status == "failed"


def test_stale_cutoff_honors_override():
    stats = recover_orphans(stale_after_seconds=10_000_000)
    assert stats.runs_closed == 0 and stats.jobs_failed == 0
