"""The task-routing seam: defaults must match the §8 routing guide, and
the local tier must never receive tool-calling work."""

from __future__ import annotations

import pytest

from relay.guardrails.harness import BudgetExceeded, RunHarness
from relay.routing.executors import execute
from relay.routing.router import ComputeTier, TaskType, route


@pytest.mark.parametrize(
    ("task_type", "tier", "extended"),
    [
        (TaskType.ENRICHMENT, ComputeTier.LOCAL, False),
        (TaskType.FIELD_EXTRACTION, ComputeTier.LOCAL, False),
        (TaskType.CLASSIFICATION, ComputeTier.LOCAL, False),
        (TaskType.TAGGING, ComputeTier.LOCAL, False),
        (TaskType.SUMMARIZATION, ComputeTier.LOCAL, False),
        (TaskType.FIT_SCORING, ComputeTier.LOCAL, False),
        (TaskType.REPLY_TRIAGE, ComputeTier.LOCAL, False),
        (TaskType.OUTREACH_COPY, ComputeTier.HOSTED, True),
        (TaskType.ORCHESTRATION, ComputeTier.HOSTED, True),
        (TaskType.SENSITIVE, ComputeTier.HOSTED, True),
    ],
)
def test_default_routes_match_spec(task_type, tier, extended):
    decision = route(task_type)
    assert decision.tier is tier
    assert decision.extended_reasoning is extended


@pytest.mark.parametrize("task_type", [TaskType.FIT_SCORING, TaskType.REPLY_TRIAGE])
def test_ambiguous_cases_escalate_to_hosted(task_type):
    decision = route(task_type, ambiguous=True)
    assert decision.tier is ComputeTier.HOSTED
    assert decision.extended_reasoning is True


def test_ambiguity_does_not_escalate_other_tasks():
    decision = route(TaskType.ENRICHMENT, ambiguous=True)
    assert decision.tier is ComputeTier.LOCAL


@pytest.mark.parametrize(
    "task_type",
    [
        TaskType.ENRICHMENT,
        TaskType.CLASSIFICATION,
        TaskType.FIT_SCORING,
        TaskType.REPLY_TRIAGE,
    ],
)
def test_tool_calling_never_routes_local(task_type):
    """§8 open validation item: until local tool-calling is validated
    (Phase 2), tool-calling steps route to the hosted tier."""
    decision = route(task_type, requires_tools=True)
    assert decision.tier is ComputeTier.HOSTED


def test_customer_facing_output_never_routes_local():
    assert route(TaskType.OUTREACH_COPY).tier is ComputeTier.HOSTED


def test_executor_bills_the_harness(tenant_a):
    tenant_id, _ = tenant_a
    harness = RunHarness(tenant_id=tenant_id, kind="routing_test", budget_units=10.0)
    execute(TaskType.ENRICHMENT, harness=harness)  # local: 0.1
    execute(TaskType.OUTREACH_COPY, harness=harness)  # hosted+ext: 3.0
    assert harness.cost_units == pytest.approx(3.1)
    harness.complete()


def test_over_budget_task_is_billed_before_it_runs(tenant_a):
    tenant_id, _ = tenant_a
    harness = RunHarness(tenant_id=tenant_id, kind="routing_test", budget_units=0.05)
    with pytest.raises(BudgetExceeded):
        execute(TaskType.ENRICHMENT, harness=harness)
