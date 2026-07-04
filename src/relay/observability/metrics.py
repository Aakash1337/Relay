"""Tenant metrics, derived on read from the canonical datastore."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from relay.db.engine import tenant_session
from relay.db.models import Lead, PipelineRun, Reply, SendJob, Suppression

#: The rolling window for rate-style metrics.
WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class TenantMetrics:
    tenant_id: uuid.UUID
    generated_at: datetime
    #: Lead count per state (full population).
    lead_states: dict[str, int] = field(default_factory=dict)
    #: Pipeline runs in the window, per status.
    runs: dict[str, int] = field(default_factory=dict)
    #: Guardrail cost spent in the window.
    cost_units_window: float = 0.0
    #: Send jobs per status (full population — queue depth lives here).
    send_jobs: dict[str, int] = field(default_factory=dict)
    replies_window: int = 0
    sent_window: int = 0
    suppression_entries: int = 0

    @property
    def run_error_rate(self) -> float | None:
        total = sum(self.runs.values())
        if not total:
            return None
        bad = sum(
            n
            for status, n in self.runs.items()
            if status not in ("completed", "running")
        )
        return bad / total

    @property
    def reply_rate(self) -> float | None:
        if not self.sent_window:
            return None
        return self.replies_window / self.sent_window


def tenant_metrics(tenant_id: uuid.UUID) -> TenantMetrics:
    cutoff = datetime.now(tz=UTC) - WINDOW
    with tenant_session(tenant_id) as session:
        lead_states = dict(
            session.execute(select(Lead.state, func.count()).group_by(Lead.state)).all()
        )
        runs = dict(
            session.execute(
                select(PipelineRun.status, func.count())
                .where(PipelineRun.started_at >= cutoff)
                .group_by(PipelineRun.status)
            ).all()
        )
        cost = float(
            session.execute(
                select(func.coalesce(func.sum(PipelineRun.cost_units), 0)).where(
                    PipelineRun.started_at >= cutoff
                )
            ).scalar_one()
        )
        send_jobs = dict(
            session.execute(
                select(SendJob.status, func.count()).group_by(SendJob.status)
            ).all()
        )
        sent_window = session.execute(
            select(func.count()).where(
                SendJob.status == "sent", SendJob.completed_at >= cutoff
            )
        ).scalar_one()
        replies_window = session.execute(
            select(func.count()).where(Reply.received_at >= cutoff)
        ).scalar_one()
        suppression = session.execute(
            select(func.count()).select_from(Suppression)
        ).scalar_one()

    return TenantMetrics(
        tenant_id=tenant_id,
        generated_at=datetime.now(tz=UTC),
        lead_states=lead_states,
        runs=runs,
        cost_units_window=cost,
        send_jobs=send_jobs,
        replies_window=replies_window,
        sent_window=sent_window,
        suppression_entries=suppression,
    )


def prometheus_text(metrics: TenantMetrics) -> str:
    """Render in Prometheus exposition format (hand-rolled on purpose —
    the shape is trivial and a client library would be a new dependency
    on the serving path)."""
    t = str(metrics.tenant_id)
    lines: list[str] = [
        "# TYPE relay_leads gauge",
        *(
            f'relay_leads{{tenant="{t}",state="{s}"}} {n}'
            for s, n in sorted(metrics.lead_states.items())
        ),
        "# TYPE relay_runs_window counter",
        *(
            f'relay_runs_window{{tenant="{t}",status="{s}"}} {n}'
            for s, n in sorted(metrics.runs.items())
        ),
        "# TYPE relay_cost_units_window gauge",
        f'relay_cost_units_window{{tenant="{t}"}} {metrics.cost_units_window}',
        "# TYPE relay_send_jobs gauge",
        *(
            f'relay_send_jobs{{tenant="{t}",status="{s}"}} {n}'
            for s, n in sorted(metrics.send_jobs.items())
        ),
        "# TYPE relay_replies_window counter",
        f'relay_replies_window{{tenant="{t}"}} {metrics.replies_window}',
        "# TYPE relay_sent_window counter",
        f'relay_sent_window{{tenant="{t}"}} {metrics.sent_window}',
        "# TYPE relay_suppression_entries gauge",
        f'relay_suppression_entries{{tenant="{t}"}} {metrics.suppression_entries}',
    ]
    return "\n".join(lines) + "\n"
