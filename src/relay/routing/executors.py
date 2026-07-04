"""Task execution behind the routing seam — real backends since Phase 1A.

The seam promised in Phase 0 holds: call sites still say
``execute(task_type, payload, harness=...)`` and get a ``TaskResult``;
what changed is that the body now routes to a real compute backend
(offline / local OpenAI-compatible / hosted Claude) selected purely by
configuration, with the §11 prompt scaffolding applied on the way in and
the output contract validated on the way out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from relay.compute.base import require_fields
from relay.compute.prompting import build_request
from relay.compute.registry import backend_for
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
    backend: str = ""
    model: str = ""


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
    """Route, execute on the tier's backend, and bill under the harness."""
    decision = route(task_type, ambiguous=ambiguous, requires_tools=requires_tools)
    backend = backend_for(decision.tier)
    request = build_request(
        task_type,
        payload,
        extended_reasoning=decision.extended_reasoning,
        max_output_tokens=get_settings().compute_max_output_tokens,
    )
    cost = _stub_cost(decision)
    # Bill BEFORE producing output: an over-budget task never runs.
    harness.spend(cost, what=str(task_type))
    response = backend.complete(request)
    require_fields(response.output, request.output_fields, backend=backend.name)
    log.info(
        "task executed",
        task_type=str(task_type),
        tier=str(decision.tier),
        backend=response.backend,
        model=response.model,
        extended_reasoning=decision.extended_reasoning,
        cost_units=cost,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
    )
    return TaskResult(
        task_type=task_type,
        decision=decision,
        output=response.output,
        cost_units=cost,
        backend=response.backend,
        model=response.model,
    )
