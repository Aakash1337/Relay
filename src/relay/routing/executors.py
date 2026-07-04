"""Stub executors for the two compute tiers (Phase 0).

No model is called anywhere in Phase 0 — these return canned,
deterministic outputs and account stub costs against the run budget, so
the routing seam and the guardrail accounting are real even though the
reasoning is not yet.

Phase 1A replaces the bodies (local model / hosted API) without touching
any call site: that is the point of the seam.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from relay.config import get_settings
from relay.guardrails.harness import RunHarness
from relay.logs import get_logger
from relay.routing.router import ComputeTier, RoutingDecision, TaskType, route

log = get_logger(__name__)


@dataclass(frozen=True)
class TaskResult:
    task_type: TaskType
    decision: RoutingDecision
    output: dict[str, Any] = field(default_factory=dict)
    cost_units: float = 0.0


_CANNED_OUTPUTS: dict[TaskType, dict[str, Any]] = {
    TaskType.ENRICHMENT: {
        "company_summary": "stub: enrichment pending Phase 1A",
        "signals": [],
    },
    TaskType.FIELD_EXTRACTION: {"fields": {}},
    TaskType.CLASSIFICATION: {"label": "pass", "confidence": 1.0},
    TaskType.TAGGING: {"tags": ["synthetic"]},
    TaskType.SUMMARIZATION: {"summary": "stub summary"},
    TaskType.FIT_SCORING: {"fit_score": 0.82, "rationale": "stub scoring"},
    TaskType.REPLY_TRIAGE: {"category": "interested", "confidence": 1.0},
    TaskType.OUTREACH_COPY: {
        "subject": "stub subject",
        "body": "stub body (personalization lands in Phase 1A)",
        "personalization_sources": {},
    },
    TaskType.ORCHESTRATION: {"plan": []},
    TaskType.SENSITIVE: {"result": None},
}


def _stub_cost(decision: RoutingDecision) -> float:
    settings = get_settings()
    if decision.tier is ComputeTier.LOCAL:
        return settings.cost_local_units
    if decision.extended_reasoning:
        return settings.cost_hosted_extended_units
    return settings.cost_hosted_units


def execute(
    task_type: TaskType,
    payload: dict[str, Any] | None = None,
    *,
    harness: RunHarness,
    ambiguous: bool = False,
    requires_tools: bool = False,
) -> TaskResult:
    """Route, 'execute', and bill one task under the harness's budget."""
    decision = route(task_type, ambiguous=ambiguous, requires_tools=requires_tools)
    cost = _stub_cost(decision)
    # Bill BEFORE producing output: an over-budget task never runs.
    harness.spend(cost, what=str(task_type))
    output = dict(_CANNED_OUTPUTS[task_type])
    if payload:
        output["_echo"] = {k: v for k, v in payload.items() if k != "raw"}
    log.info(
        "task executed",
        task_type=str(task_type),
        tier=str(decision.tier),
        extended_reasoning=decision.extended_reasoning,
        cost_units=cost,
    )
    return TaskResult(
        task_type=task_type,
        decision=decision,
        output=output,
        cost_units=cost,
    )
