"""Alert evaluation (Phase 2) — failures and spend spikes get loud.

Rules are dumb thresholds by design, like the guardrails: they must
keep working when the intelligent parts are the thing that broke.
Evaluation is on-read (call it from a schedule or the /alerts endpoint);
firing goes to the structured log always, and to a webhook when one is
configured. The webhook is best-effort — alerting must never take down
the thing it watches.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select

from relay.config import get_settings
from relay.db.engine import tenant_session
from relay.db.models import PipelineRun, SendJob
from relay.logs import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class Alert:
    rule: str
    severity: str  # "warning" | "critical"
    detail: str
    value: float


def evaluate_alerts(tenant_id: uuid.UUID) -> list[Alert]:
    settings = get_settings()
    alerts: list[Alert] = []
    now = datetime.now(tz=UTC)

    with tenant_session(tenant_id) as session:
        # ── Spend spike: guardrail units burned in the last hour ───────────
        hour_cost = float(
            session.execute(
                select(func.coalesce(func.sum(PipelineRun.cost_units), 0)).where(
                    PipelineRun.started_at >= now - timedelta(hours=1)
                )
            ).scalar_one()
        )
        if hour_cost > settings.alert_spend_units_per_hour:
            alerts.append(
                Alert(
                    rule="spend_spike",
                    severity="critical",
                    detail=(
                        f"{hour_cost:.1f} cost units in the last hour "
                        f"(threshold {settings.alert_spend_units_per_hour})"
                    ),
                    value=hour_cost,
                )
            )

        # ── Failure streak: latest N finished runs all bad ──────────────────
        streak_n = settings.alert_failure_streak
        recent = (
            session.execute(
                select(PipelineRun.status)
                .where(PipelineRun.status != "running")
                .order_by(PipelineRun.started_at.desc())
                .limit(streak_n)
            )
            .scalars()
            .all()
        )
        if len(recent) == streak_n and all(s != "completed" for s in recent):
            alerts.append(
                Alert(
                    rule="failure_streak",
                    severity="critical",
                    detail=f"last {streak_n} finished runs all failed/killed",
                    value=float(streak_n),
                )
            )

        # ── Stuck queue: queued jobs nobody is picking up ───────────────────
        stale_cutoff = now - timedelta(seconds=settings.alert_queue_stale_seconds)
        stuck = session.execute(
            select(func.count()).where(
                SendJob.status == "queued", SendJob.queued_at < stale_cutoff
            )
        ).scalar_one()
        if stuck:
            alerts.append(
                Alert(
                    rule="queue_stuck",
                    severity="warning",
                    detail=(
                        f"{stuck} send job(s) queued longer than "
                        f"{settings.alert_queue_stale_seconds}s — worker down?"
                    ),
                    value=float(stuck),
                )
            )

    for alert in alerts:
        log.warning(
            "ALERT",
            rule=alert.rule,
            severity=alert.severity,
            detail=alert.detail,
            tenant_id=str(tenant_id),
        )
    if alerts and settings.alert_webhook_url:
        _post_webhook(tenant_id, alerts, settings.alert_webhook_url)
    return alerts


def _post_webhook(tenant_id: uuid.UUID, alerts: list[Alert], url: str) -> None:
    """Best-effort delivery; alerting must never crash the caller."""
    payload = {
        "tenant_id": str(tenant_id),
        "alerts": [
            {
                "rule": a.rule,
                "severity": a.severity,
                "detail": a.detail,
                "value": a.value,
            }
            for a in alerts
        ],
    }
    try:
        httpx.post(url, json=payload, timeout=5.0)
    except Exception as exc:  # noqa: BLE001 — logged, never raised
        log.warning("alert webhook delivery failed", error=str(exc))
