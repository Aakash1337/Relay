"""The guardrail harness — dumb limits OUTSIDE the planner (§9).

The failure mode these defend against is confident, expensive
persistence: a loop that keeps going while stuck. The harness enforces
mechanical stops that hold even if the intelligent component is the
thing malfunctioning:

- max-iteration counter (the single most important guardrail),
- per-run budget ceiling (cost units),
- run bookkeeping in ``pipeline_runs`` so a kill is visible and audited.

The planner may do smart recovery inside a step; it cannot talk its way
past these limits, because they are counted here, not by it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func

from relay.config import get_settings
from relay.db.engine import tenant_session
from relay.db.models import PipelineRun
from relay.logs import get_logger

log = get_logger(__name__)


class GuardrailViolation(Exception):
    """Base class: a dumb limit fired."""


class IterationCapExceeded(GuardrailViolation):
    pass


class BudgetExceeded(GuardrailViolation):
    pass


class RunHarness:
    """Guardrailed execution context for one pipeline run.

    Counters live in process; the kill (or completion) is persisted to
    ``pipeline_runs`` in its own transaction, so the record survives even
    when the offending step's transaction rolls back.
    """

    def __init__(
        self,
        *,
        tenant_id: uuid.UUID,
        kind: str,
        lead_id: uuid.UUID | None = None,
        max_iterations: int | None = None,
        budget_units: float | None = None,
    ) -> None:
        settings = get_settings()
        self.tenant_id = tenant_id
        self.kind = kind
        self.lead_id = lead_id
        # `is None` (not truthiness): an explicit 0 must be honored/rejected,
        # not silently replaced by the default — a zero budget is exactly the
        # "no spend allowed" the harness exists to enforce.
        self.max_iterations = (
            settings.max_iterations_default
            if max_iterations is None
            else max_iterations
        )
        self.budget_units = (
            settings.budget_units_default if budget_units is None else budget_units
        )
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be >= 1")
        if self.budget_units < 0:
            raise ValueError("budget_units must be >= 0")
        self.iterations = 0
        self.cost_units = 0.0
        self._pending_kill: tuple[str, str] | None = None
        self.run_id = self._create_run_row()

    # ── Bookkeeping ─────────────────────────────────────────────────────────

    def _create_run_row(self) -> uuid.UUID:
        with tenant_session(self.tenant_id) as session:
            run = PipelineRun(
                tenant_id=self.tenant_id,
                kind=self.kind,
                lead_id=self.lead_id,
                max_iterations=self.max_iterations,
                budget_units=self.budget_units,
            )
            session.add(run)
            session.flush()
            run_id = run.id
        log.info(
            "run started",
            run_id=str(run_id),
            kind=self.kind,
            max_iterations=self.max_iterations,
            budget_units=self.budget_units,
        )
        return run_id

    def _persist(self, status: str, detail: str | None = None) -> None:
        with tenant_session(self.tenant_id) as session:
            run = session.get(PipelineRun, self.run_id)
            if run is None:  # pragma: no cover — run rows are never deleted
                return
            run.iterations = self.iterations
            run.cost_units = self.cost_units
            run.status = status
            run.detail = detail
            if status != "running":
                run.finished_at = func.now()

    # ── The dumb limits ─────────────────────────────────────────────────────
    #
    # tick()/spend() are called from inside a step's open DB session. They do
    # NOT persist the kill inline — that would open a second connection while
    # the step still holds one, so under concurrent load the guardrail kill
    # could block on pool exhaustion exactly when it must run. Instead they
    # record the pending kill and raise; the caller persists it via
    # finalize_kill() AFTER the step session has closed (one connection).

    def tick(self, step: str) -> None:
        """Count one iteration; kill the run past the cap."""
        self.iterations += 1
        if self.iterations > self.max_iterations:
            self._pending_kill = (
                "killed_iteration_cap",
                f"iteration cap {self.max_iterations} exceeded at step {step}",
            )
            log.error(
                "run killed: iteration cap",
                run_id=str(self.run_id),
                step=step,
                iterations=self.iterations,
            )
            raise IterationCapExceeded(
                f"run {self.run_id}: iteration cap {self.max_iterations} "
                f"exceeded at step {step!r}"
            )

    def spend(self, units: float, what: str) -> None:
        """Account cost; kill the run past the budget ceiling."""
        self.cost_units += units
        if self.cost_units > self.budget_units:
            self._pending_kill = (
                "killed_budget",
                f"budget {self.budget_units} exceeded by {what}",
            )
            log.error(
                "run killed: budget ceiling",
                run_id=str(self.run_id),
                what=what,
                cost_units=self.cost_units,
            )
            raise BudgetExceeded(
                f"run {self.run_id}: budget {self.budget_units} exceeded "
                f"({self.cost_units} spent, last: {what!r})"
            )

    def finalize_kill(self) -> None:
        """Persist a pending guardrail kill. Call after the step session
        that triggered it has closed (keeps the write to one connection)."""
        if self._pending_kill is None:
            return
        status, detail = self._pending_kill
        self._pending_kill = None
        self._persist(status, detail)

    # ── Terminal bookkeeping ────────────────────────────────────────────────

    def complete(self, detail: str | None = None) -> None:
        self._persist("completed", detail)
        log.info(
            "run completed",
            run_id=str(self.run_id),
            iterations=self.iterations,
            cost_units=self.cost_units,
        )

    def fail(self, detail: str) -> None:
        self._persist("failed", detail)
        log.error("run failed", run_id=str(self.run_id), detail=detail)
