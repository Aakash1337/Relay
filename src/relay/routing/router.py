"""The task-routing seam (§8) — built day one, stubbed until Phase 1A.

Routes each task type to a compute tier with an extended-reasoning
default, per the routing guide in the project documentation. The seam
exists now because retrofitting it later is painful; the executors behind
it are stubs until the two-tier split goes live.

Hard rule encoded from §8's open validation item: the local tier is NOT
trusted to call tools. Any task that requires tool calls routes to the
hosted tier regardless of its default, until local tool-calling
reliability has been validated separately (Phase 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TaskType(StrEnum):
    ENRICHMENT = "enrichment"
    FIELD_EXTRACTION = "field_extraction"
    CLASSIFICATION = "classification"
    TAGGING = "tagging"
    SUMMARIZATION = "summarization"
    FIT_SCORING = "fit_scoring"
    REPLY_TRIAGE = "reply_triage"
    OUTREACH_COPY = "outreach_copy"
    ORCHESTRATION = "orchestration"
    #: Anything touching credentials or untrusted input.
    SENSITIVE = "sensitive"


class ComputeTier(StrEnum):
    LOCAL = "local"
    HOSTED = "hosted"


@dataclass(frozen=True)
class RoutingDecision:
    task_type: TaskType
    tier: ComputeTier
    extended_reasoning: bool
    reason: str


#: Defaults from the §8 routing guide (task → tier → extended reasoning).
DEFAULT_ROUTES: dict[TaskType, tuple[ComputeTier, bool]] = {
    TaskType.ENRICHMENT: (ComputeTier.LOCAL, False),
    TaskType.FIELD_EXTRACTION: (ComputeTier.LOCAL, False),
    TaskType.CLASSIFICATION: (ComputeTier.LOCAL, False),
    TaskType.TAGGING: (ComputeTier.LOCAL, False),
    TaskType.SUMMARIZATION: (ComputeTier.LOCAL, False),
    # Ambiguous cases escalate — see route().
    TaskType.FIT_SCORING: (ComputeTier.LOCAL, False),
    TaskType.REPLY_TRIAGE: (ComputeTier.LOCAL, False),
    # Customer-facing output: hosted, extended reasoning on.
    TaskType.OUTREACH_COPY: (ComputeTier.HOSTED, True),
    # Decisions that cascade: hosted, extended reasoning on.
    TaskType.ORCHESTRATION: (ComputeTier.HOSTED, True),
    TaskType.SENSITIVE: (ComputeTier.HOSTED, True),
}


def route(
    task_type: TaskType,
    *,
    ambiguous: bool = False,
    requires_tools: bool = False,
) -> RoutingDecision:
    """Route one task. Hard-coded per type; overridden only at the margins."""
    tier, extended = DEFAULT_ROUTES[task_type]
    reason = "default route"

    if ambiguous and task_type in (
        TaskType.FIT_SCORING,
        TaskType.REPLY_TRIAGE,
    ):
        tier, extended = ComputeTier.HOSTED, True
        reason = "ambiguous case escalated to hosted with extended reasoning"

    if requires_tools and tier is ComputeTier.LOCAL:
        tier = ComputeTier.HOSTED
        reason = (
            "local tool-calling not yet validated (§8 open item) — forced to hosted"
        )

    return RoutingDecision(task_type, tier, extended, reason)
