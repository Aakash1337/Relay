"""Phase 0 exit gate: an empty pipeline moves a fake lead through every
state; the log traces its full journey; reprocessing is a no-op."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from relay.db.engine import tenant_session
from relay.db.models import Lead, LeadTransition, PipelineRun, SendJob
from relay.domain.states import HAPPY_PATH
from relay.pipeline.runner import PipelineRunner
from tests.conftest import walk_to_closed

pytestmark = pytest.mark.exit_gate


def _trace(tenant_id, lead_id) -> list[tuple[str, str]]:
    with tenant_session(tenant_id) as session:
        rows = (
            session.execute(
                select(LeadTransition)
                .where(LeadTransition.lead_id == lead_id)
                .order_by(LeadTransition.created_at, LeadTransition.id)
            )
            .scalars()
            .all()
        )
        return [(r.from_state, r.to_state) for r in rows]


def test_fake_lead_walks_every_state(tenant_a, factory_a, capsys):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()

    walk_to_closed(tenant_id, lead_id)

    # The DB trace IS the journey: every happy-path edge, in order.
    trace = _trace(tenant_id, lead_id)
    expected = [
        (str(a), str(b)) for a, b in zip(HAPPY_PATH, HAPPY_PATH[1:], strict=False)
    ]
    assert trace == expected

    # The structured log also traces the journey (observability gate).
    err = capsys.readouterr().err
    logged = [
        (event.get("from_state"), event.get("to_state"))
        for event in (
            json.loads(line) for line in err.splitlines() if line.startswith("{")
        )
        if event.get("event") == "transition" and event.get("lead_id") == str(lead_id)
    ]
    assert logged == expected

    # Every transition is attributable: pipeline, human, or worker.
    with tenant_session(tenant_id) as session:
        actors = set(
            session.execute(
                select(LeadTransition.actor).where(LeadTransition.lead_id == lead_id)
            ).scalars()
        )
    assert actors == {
        "system:pipeline",
        "human:test-operator",
        "worker:send",
    }


def test_journey_runs_are_guardrailed_and_completed(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    walk_to_closed(tenant_id, lead_id)

    with tenant_session(tenant_id) as session:
        runs = session.execute(select(PipelineRun)).scalars().all()
        assert runs, "journey must be tracked in pipeline_runs"
        assert {r.status for r in runs} == {"completed"}
        assert all(r.iterations > 0 for r in runs if r.kind == "lead_journey")


def test_reprocessing_closed_lead_is_noop(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    walk_to_closed(tenant_id, lead_id)
    before = _trace(tenant_id, lead_id)

    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()

    assert outcome.final_state == "closed"
    assert outcome.stopped_on == "terminal"
    assert outcome.visited == []
    assert _trace(tenant_id, lead_id) == before  # not one new transition

    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None and lead.state == "closed"
        jobs = session.execute(select(func.count()).select_from(SendJob)).scalar_one()
        assert jobs == 1  # still exactly one send job


def test_rerun_mid_pipeline_does_not_duplicate_steps(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()

    first = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert first.stopped_on == "waiting_human"
    before = _trace(tenant_id, lead_id)

    second = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert second.stopped_on == "waiting_human"
    assert second.visited == []
    assert _trace(tenant_id, lead_id) == before
