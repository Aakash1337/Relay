"""Prototype utilities: region-rules seam, admin console, benchmark."""

from __future__ import annotations

import pytest

from relay.config import get_settings
from relay.pipeline.runner import PipelineRunner
from tests.conftest import approve_current_draft, run_to_approval

pytestmark = pytest.mark.exit_gate


# ── Region-specific lawful-basis rules (the Legal/Data Preflight seam) ──────


def test_region_rules_block_a_disallowed_basis(tenant_a, factory_a, monkeypatch):
    """With rules configured, a lead whose region does not allow its
    lawful basis dies at the eligibility gate."""
    monkeypatch.setenv(
        "RELAY_REGION_BASIS_RULES", '{"EU": ["consent", "test_consent"]}'
    )
    get_settings.cache_clear()
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead(region_assumption="EU")  # synthetic basis
    run_to_approval(tenant_id, lead_id)
    approve_current_draft(tenant_id, lead_id)

    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.final_state == "send_blocked"
    monkeypatch.delenv("RELAY_REGION_BASIS_RULES")
    get_settings.cache_clear()


def test_unlisted_region_is_blocked_when_rules_exist(tenant_a, factory_a, monkeypatch):
    """Over-blocking is the safe direction: once ANY rules are configured,
    a region absent from the map is uncleared and blocked."""
    monkeypatch.setenv("RELAY_REGION_BASIS_RULES", '{"US": ["synthetic"]}')
    get_settings.cache_clear()
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead(region_assumption="EU")
    run_to_approval(tenant_id, lead_id)
    approve_current_draft(tenant_id, lead_id)

    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.final_state == "send_blocked"
    monkeypatch.delenv("RELAY_REGION_BASIS_RULES")
    get_settings.cache_clear()


def test_allowed_region_basis_passes(tenant_a, factory_a, monkeypatch):
    monkeypatch.setenv("RELAY_REGION_BASIS_RULES", '{"EU": ["synthetic"]}')
    get_settings.cache_clear()
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead(region_assumption="EU")
    run_to_approval(tenant_id, lead_id)
    approve_current_draft(tenant_id, lead_id)

    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.stopped_on == "waiting_worker", outcome
    monkeypatch.delenv("RELAY_REGION_BASIS_RULES")
    get_settings.cache_clear()


def test_malformed_region_rules_fail_loudly(monkeypatch):
    """A silently ignored compliance rule is worse than a crash."""
    import json

    monkeypatch.setenv("RELAY_REGION_BASIS_RULES", "not-json")
    get_settings.cache_clear()
    with pytest.raises(json.JSONDecodeError):
        get_settings().region_rules()
    monkeypatch.delenv("RELAY_REGION_BASIS_RULES")
    get_settings.cache_clear()


# ── Admin console page ──────────────────────────────────────────────────────


def test_admin_page_serves_and_is_token_gated_server_side(client):
    page = client.get("/admin")
    assert page.status_code == 200
    assert "RELAY admin console" in page.text
    assert "/internal/tenants/onboard" in page.text
    # The page adds no capability: the endpoints it drives still demand
    # the admin token.
    assert client.post("/internal/tenants/onboard", json={}).status_code == 422


# ── Benchmark harness ───────────────────────────────────────────────────────


def test_benchmark_runs_end_to_end(capsys):
    """A tiny benchmark run completes and reports every phase — the
    instrument for the Phase 4 throughput exit-gate item works."""
    from scripts.benchmark_throughput import run_benchmark

    timings = run_benchmark(tenants=1, leads=2, concurrency=2)
    assert set(timings) == {
        "pipeline_to_gate",
        "approve_all",
        "queue_eligibility",
        "worker_drain",
        "total",
    }
    out = capsys.readouterr().out
    assert "leads/sec" in out
