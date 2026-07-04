"""Phase 0 exit gate: a forced infinite loop is stopped by the iteration
cap; an over-budget run by the budget ceiling. Kill switches proven
before anything autonomous runs."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from relay.db.engine import tenant_session
from relay.db.models import PipelineRun
from relay.guardrails.harness import (
    BudgetExceeded,
    IterationCapExceeded,
    RunHarness,
)
from relay.pipeline.runner import PipelineRunner

pytestmark = pytest.mark.exit_gate


def _run_status(tenant_id, run_id) -> str:
    with tenant_session(tenant_id) as session:
        run = session.execute(
            select(PipelineRun).where(PipelineRun.id == run_id)
        ).scalar_one()
        return run.status


def test_forced_infinite_loop_is_killed_by_iteration_cap(tenant_a):
    tenant_id, _ = tenant_a
    harness = RunHarness(tenant_id=tenant_id, kind="forced_loop", max_iterations=25)

    with pytest.raises(IterationCapExceeded):
        while True:  # the malfunction the harness exists for
            harness.tick("spin")

    assert harness.iterations == 26  # cap + the tick that tripped it
    assert _run_status(tenant_id, harness.run_id) == "killed_iteration_cap"


def test_over_budget_run_is_killed_by_budget_ceiling(tenant_a):
    tenant_id, _ = tenant_a
    harness = RunHarness(tenant_id=tenant_id, kind="budget_burn", budget_units=5.0)

    with pytest.raises(BudgetExceeded):
        while True:
            harness.spend(1.0, what="expensive_stub_task")

    assert harness.cost_units == pytest.approx(6.0)
    assert _run_status(tenant_id, harness.run_id) == "killed_budget"


def test_pipeline_run_killed_by_tiny_iteration_cap(tenant_a, factory_a):
    """The real runner (not a synthetic loop) also dies at the cap."""
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    runner = PipelineRunner(tenant_id, lead_id=lead_id, max_iterations=3)

    with pytest.raises(IterationCapExceeded):
        runner.run()

    assert _run_status(tenant_id, runner.harness.run_id) == ("killed_iteration_cap")


def test_pipeline_run_killed_by_tiny_budget(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    # First routed task costs 0.1 (local); second (enrichment) trips 0.15.
    runner = PipelineRunner(tenant_id, lead_id=lead_id, budget_units=0.15)

    with pytest.raises(BudgetExceeded):
        runner.run()

    assert _run_status(tenant_id, runner.harness.run_id) == "killed_budget"


def test_killed_run_leaves_lead_in_consistent_state(tenant_a, factory_a):
    """A guardrail kill never leaves a half-applied step: the step's
    transaction rolls back; earlier completed steps stay."""
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    runner = PipelineRunner(tenant_id, lead_id=lead_id, max_iterations=3)
    with pytest.raises(IterationCapExceeded):
        runner.run()

    from relay.db.models import Lead

    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None
        # 3 completed steps: created→source_checked→enrichment_pending→
        # enriched; the 4th step died before its transition committed.
        assert lead.state == "enriched"
