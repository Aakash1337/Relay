"""Observability: metrics derived from real rows, alerts that fire."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from relay.config import get_settings
from relay.db.engine import admin_engine
from relay.guardrails.harness import RunHarness
from relay.observability import evaluate_alerts, prometheus_text, tenant_metrics
from tests.conftest import walk_to_closed

pytestmark = pytest.mark.exit_gate


@pytest.fixture(autouse=True)
def _settings_reset():
    yield
    get_settings.cache_clear()


# ── Metrics ──────────────────────────────────────────────────────────────────


def test_metrics_reflect_a_real_journey(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    walk_to_closed(tenant_id, lead_id)

    m = tenant_metrics(tenant_id)
    assert m.lead_states.get("closed") == 1
    assert m.send_jobs.get("sent") == 1
    assert m.sent_window == 1
    assert m.replies_window == 1
    assert m.cost_units_window > 0
    assert m.runs.get("completed", 0) >= 2
    assert m.run_error_rate == 0.0
    assert m.reply_rate == 1.0


def test_prometheus_rendering(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    factory_a.lead()
    body = prometheus_text(tenant_metrics(tenant_id))
    assert f'relay_leads{{tenant="{tenant_id}",state="created"}} 1' in body
    assert "# TYPE relay_cost_units_window gauge" in body


# ── Alerts ───────────────────────────────────────────────────────────────────


def test_no_alerts_on_healthy_tenant(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    walk_to_closed(tenant_id, factory_a.lead())
    assert evaluate_alerts(tenant_id) == []


def test_failure_streak_alert_fires(tenant_a):
    tenant_id, _ = tenant_a
    for _ in range(3):
        harness = RunHarness(tenant_id=tenant_id, kind="doomed")
        harness.fail("synthetic failure")
    fired = evaluate_alerts(tenant_id)
    assert any(a.rule == "failure_streak" for a in fired)


def test_spend_spike_alert_fires(tenant_a, factory_a, monkeypatch):
    tenant_id, _ = tenant_a
    monkeypatch.setenv("RELAY_ALERT_SPEND_UNITS_PER_HOUR", "0.5")
    get_settings.cache_clear()
    walk_to_closed(tenant_id, factory_a.lead())  # burns > 0.5 units

    fired = evaluate_alerts(tenant_id)
    spike = [a for a in fired if a.rule == "spend_spike"]
    assert spike and spike[0].severity == "critical"


def test_queue_stuck_alert_fires(tenant_a, factory_a):
    from tests.conftest import approve_current_draft, run_to_approval

    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    run_to_approval(tenant_id, lead_id)
    approve_current_draft(tenant_id, lead_id)
    from relay.pipeline.runner import PipelineRunner

    PipelineRunner(tenant_id, lead_id=lead_id).run()  # queues, worker not run
    with admin_engine().begin() as conn:
        conn.execute(text("UPDATE send_jobs SET queued_at = now() - interval '1 hour'"))
    fired = evaluate_alerts(tenant_id)
    assert any(a.rule == "queue_stuck" for a in fired)


def test_alert_webhook_receives_payload(tenant_a, monkeypatch):
    tenant_id, _ = tenant_a
    received: list[dict] = []
    monkeypatch.setenv("RELAY_ALERT_WEBHOOK_URL", "http://alerts.internal/hook")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "relay.observability.alerts.httpx.post",
        lambda url, json, timeout: received.append({"url": url, "body": json}),
    )
    for _ in range(3):
        RunHarness(tenant_id=tenant_id, kind="doomed").fail("synthetic")
    evaluate_alerts(tenant_id)
    assert received and received[0]["url"] == "http://alerts.internal/hook"
    assert received[0]["body"]["alerts"][0]["rule"] == "failure_streak"


# ── HTTP surface ─────────────────────────────────────────────────────────────


def test_metrics_endpoints_over_http(client, api_tenant):
    auth = {"X-API-Key": api_tenant["api_key"]}
    res = client.get("/metrics", headers=auth)
    assert res.status_code == 200
    assert res.json()["tenant_id"] == api_tenant["id"]

    prom = client.get("/metrics/prometheus", headers=auth)
    assert prom.status_code == 200
    assert "relay_suppression_entries" in prom.text

    alerts_res = client.get("/alerts", headers=auth)
    assert alerts_res.status_code == 200
    assert alerts_res.json()["alerts"] == []


def test_metrics_require_tenant_auth(client):
    assert client.get("/metrics").status_code in (401, 403, 422)


def test_ops_page_is_served_and_self_contained(client):
    res = client.get("/ops")
    assert res.status_code == 200
    assert "RELAY ops" in res.text
    assert "<script src" not in res.text and "https://" not in res.text
