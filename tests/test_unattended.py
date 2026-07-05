"""Phase 2 exit gate: the system runs unattended on a schedule.

Simulates the n8n spine's tick loop over a mixed synthetic cohort:
every tick advances active leads, runs the send worker (which begins
with crash recovery), and runs the retention purge. Assertions:

- the cohort converges — every lead ends in a terminal state or a
  legitimate wait (human gate), with no lead stuck in limbo;
- extra ticks after convergence change NOTHING (idempotent schedule);
- nothing requires a human except the human gate itself.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from relay.db.engine import tenant_session
from relay.db.models import Lead, LeadTransition
from relay.domain.approval import review_draft
from relay.domain.states import TERMINAL_STATES, LeadState
from relay.domain.vocab import ReviewDecision
from relay.pipeline.runner import PipelineRunner
from relay.routing.router import ComputeTier, TaskType, route
from relay.synthetic.seed import seed_campaign
from relay.workers.retention_worker import run_once as retention_tick
from relay.workers.send_worker import process_pending

pytestmark = pytest.mark.exit_gate

_STABLE = set(TERMINAL_STATES) | {
    LeadState.APPROVAL_PENDING,  # legitimately waits for a human
}


def _tick(tenant_id, lead_ids) -> None:
    """One schedule tick: advance leads, work the queue, purge retention."""
    for lead_id in lead_ids:
        with tenant_session(tenant_id) as session:
            lead = session.get(Lead, lead_id)
            state = LeadState(lead.state)
        if state not in _STABLE and state is not LeadState.SEND_QUEUED:
            PipelineRunner(tenant_id, lead_id=lead_id).run()
    process_pending()  # includes the crash-recovery pass
    retention_tick()


def _states(tenant_id, lead_ids) -> dict:
    with tenant_session(tenant_id) as session:
        return {
            str(lead.id): lead.state
            for lead in session.execute(select(Lead)).scalars()
            if lead.id in set(lead_ids)
        }


def test_unattended_schedule_converges_and_stays_converged(tenant_a):
    tenant_id, _ = tenant_a
    result = seed_campaign(tenant_id, n=12, seed=2026)
    lead_ids = result.lead_ids

    # Ticks 1-3: run the schedule; approve pending drafts once mid-way
    # (the single legitimately-human act in the loop).
    _tick(tenant_id, lead_ids)
    with tenant_session(tenant_id) as session:
        from relay.db.models import OutreachDraft

        for draft in (
            session.execute(
                select(OutreachDraft).where(OutreachDraft.status == "pending_approval")
            )
            .scalars()
            .all()
        ):
            review_draft(
                session,
                draft=draft,
                reviewer="scheduled-reviewer",
                decision=ReviewDecision.APPROVED,
            )
    for _ in range(4):
        _tick(tenant_id, lead_ids)

    # Converged: every lead is terminal (replies triaged, unsubscribes
    # suppressed, bookings closed) — none stuck in an active limbo.
    states = _states(tenant_id, lead_ids)
    stuck = {k: v for k, v in states.items() if LeadState(v) not in TERMINAL_STATES}
    assert stuck == {}, f"leads stuck after unattended run: {stuck}"

    # Idempotence: two more full ticks change nothing at all.
    with tenant_session(tenant_id) as session:
        transitions_before = session.execute(select(LeadTransition)).scalars()
        count_before = len(transitions_before.all())
    _tick(tenant_id, lead_ids)
    _tick(tenant_id, lead_ids)
    with tenant_session(tenant_id) as session:
        count_after = len(session.execute(select(LeadTransition)).scalars().all())
    assert count_after == count_before
    assert _states(tenant_id, lead_ids) == states


def test_tool_requiring_tasks_never_route_local():
    """Pin for docs/decisions/local-tool-calling.md: requires_tools forces
    the hosted tier for every task type, including local-default ones."""
    for task_type in TaskType:
        decision = route(task_type, requires_tools=True)
        assert decision.tier is ComputeTier.HOSTED, task_type
