"""Observability (Phase 2): metrics, alerts, and the ops view.

Everything is derived from rows the pipeline already writes — runs,
transitions, jobs, replies, suppression. There is no separate metering
pipeline to drift out of sync, and metrics never require a new write
path (nothing to break, nothing to forge).
"""

from relay.observability.alerts import Alert, evaluate_alerts
from relay.observability.metrics import TenantMetrics, prometheus_text, tenant_metrics

__all__ = [
    "Alert",
    "TenantMetrics",
    "evaluate_alerts",
    "prometheus_text",
    "tenant_metrics",
]
