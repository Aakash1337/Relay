"""Phase 4 exit-gate shapes: self-serve onboarding, per-tenant quotas
and spend controls, cost attribution, and two tenants running
simultaneously with verified isolation."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from relay.db.engine import admin_session, tenant_session
from relay.db.models import Lead, PipelineRun, SendJob, Tenant
from relay.economics import tenant_economics
from relay.guardrails.harness import TenantSpendCapExceeded
from relay.observability import evaluate_alerts
from relay.pipeline.runner import PipelineRunner
from relay.workers.send_worker import process_pending
from tests.conftest import ADMIN, approve_current_draft, run_to_approval

pytestmark = pytest.mark.exit_gate


# ── Exit gate: a new client onboards without hand-editing config ────────────


def test_onboarding_provisions_a_working_chain(client):
    response = client.post(
        "/internal/tenants/onboard",
        headers=ADMIN,
        json={
            "name": f"onboard-{uuid.uuid4().hex[:8]}",
            "source": {
                "name": "seed-contacts",
                "source_type": "seed",
                "terms_allow_use": "yes",
            },
            "campaign": {"name": "first-campaign"},
            "daily_send_cap": 2,
            "monthly_spend_cap_units": 500,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    api_key = body["api_key"]

    # The returned chain works immediately: create a lead with it…
    lead = client.post(
        "/leads",
        headers={"X-API-Key": api_key},
        json={
            "campaign_id": body["campaign_id"],
            "source_id": body["source_id"],
            "email": f"first-{uuid.uuid4().hex[:6]}@example.test",
            "lawful_basis": "synthetic",
            "region_assumption": "EU",
        },
    )
    assert lead.status_code == 201, lead.text

    # …and the quotas landed on the tenant row, visible via /economics.
    econ = client.get("/economics", headers={"X-API-Key": api_key})
    assert econ.status_code == 200
    econ_body = econ.json()
    assert econ_body["monthly_spend_cap_units"] == 500
    assert econ_body["funnel"]["leads"] == 1

    # Admin-only surface.
    assert client.post("/internal/tenants/onboard", json={}).status_code == 422


# ── Exit gate: two tenants simultaneously, isolation verified ───────────────


def test_two_tenants_run_concurrently_with_isolation(
    tenant_a, tenant_b, factory_a, factory_b
):
    """Three leads per tenant walk the funnel CONCURRENTLY (pipelines,
    then racing workers). Both cohorts converge, and each tenant's RLS
    view contains exactly its own rows afterwards."""
    ta, _ = tenant_a
    tb, _ = tenant_b
    leads_a = [factory_a.lead() for _ in range(3)]
    leads_b = [factory_b.lead() for _ in range(3)]

    def to_queue(pair) -> None:
        tenant_id, lead_id = pair
        run_to_approval(tenant_id, lead_id)
        approve_current_draft(tenant_id, lead_id)
        outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
        assert outcome.stopped_on == "waiting_worker", outcome

    pairs = [(ta, x) for x in leads_a] + [(tb, x) for x in leads_b]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(to_queue, pairs))

    with ThreadPoolExecutor(max_workers=4) as pool:
        stats = list(pool.map(lambda _: process_pending(max_jobs=20), range(4)))
    assert sum(s.sent for s in stats) == 6

    for tenant_id, own_leads in ((ta, leads_a), (tb, leads_b)):
        with tenant_session(tenant_id) as session:
            visible_leads = set(session.execute(select(Lead.id)).scalars())
            assert visible_leads == set(own_leads)  # RLS: own rows, all of them
            job_states = session.execute(select(SendJob.status)).scalars().all()
            assert len(job_states) == 3
            assert all(s == "sent" for s in job_states)


# ── Exit gate: per-tenant spend controls ────────────────────────────────────


def test_monthly_spend_cap_refuses_new_runs_and_recovers(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()

    with admin_session() as session:
        session.get(Tenant, tenant_id).monthly_spend_cap_units = 0

    # At/over the cap, a NEW run refuses to start — as a recorded kill.
    with pytest.raises(TenantSpendCapExceeded, match="monthly cap"):
        PipelineRunner(tenant_id, lead_id=lead_id)
    with tenant_session(tenant_id) as session:
        statuses = session.execute(select(PipelineRun.status)).scalars().all()
        assert "killed_tenant_spend_cap" in statuses

    # Raising the cap restores service, and the lead is undamaged.
    with admin_session() as session:
        session.get(Tenant, tenant_id).monthly_spend_cap_units = 10_000
    run_to_approval(tenant_id, lead_id)


def test_spend_cap_alerts_before_and_at_the_wall(tenant_a, factory_a):
    """80% of the cap warns; 100% goes critical — the operator hears
    about it BEFORE the harness starts refusing runs."""
    tenant_id, _ = tenant_a
    run_to_approval(tenant_id, factory_a.lead())  # spend some real units
    with tenant_session(tenant_id) as session:
        spent = sum(
            float(v) for v in session.execute(select(PipelineRun.cost_units)).scalars()
        )
    assert spent > 0

    with admin_session() as session:
        session.get(Tenant, tenant_id).monthly_spend_cap_units = spent / 0.9
    fired = {a.rule: a for a in evaluate_alerts(tenant_id)}
    assert fired["tenant_spend_cap_approaching"].severity == "warning"
    assert "tenant_spend_cap_reached" not in fired

    with admin_session() as session:
        session.get(Tenant, tenant_id).monthly_spend_cap_units = spent
    fired = {a.rule: a for a in evaluate_alerts(tenant_id)}
    assert fired["tenant_spend_cap_reached"].severity == "critical"


# ── Exit gate: per-client cost and profitability are visible ────────────────


def test_tenant_economics_reports_cost_per_meeting_and_headroom(tenant_a, factory_a):
    from tests.conftest import walk_to_closed

    tenant_id, _ = tenant_a
    with admin_session() as session:
        session.get(Tenant, tenant_id).monthly_spend_cap_units = 1_000
    walk_to_closed(tenant_id, factory_a.lead())  # reaches booked → closed

    report = tenant_economics(tenant_id)
    assert report.meetings_booked == 1
    assert report.cost_units_total > 0
    assert report.cost_units_per_meeting == pytest.approx(report.cost_units_total)
    assert report.funnel["leads"] == 1 and report.funnel["booked"] == 1
    assert report.monthly_spend_cap_units == 1_000
    assert report.spend_cap_remaining_units == pytest.approx(
        1_000 - report.cost_units_30d
    )


# ── Exit gate: throughput under concurrent multi-tenant load ────────────────


def test_concurrent_worker_drains_multiple_tenants_in_one_pass(
    tenant_a, tenant_b, factory_a, factory_b
):
    """The scaled worker (tenants in parallel threads) drains a mixed
    multi-tenant queue in a single pass with the same semantics as the
    serial worker: per-tenant FIFO, per-job transactions, exact
    isolation afterwards."""
    ta, _ = tenant_a
    tb, _ = tenant_b
    cohorts = {ta: [], tb: []}
    for tenant_id, factory in ((ta, factory_a), (tb, factory_b)):
        for _ in range(4):
            lead_id = factory.lead()
            run_to_approval(tenant_id, lead_id)
            approve_current_draft(tenant_id, lead_id)
            outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
            assert outcome.stopped_on == "waiting_worker", outcome
            cohorts[tenant_id].append(lead_id)

    stats = process_pending(max_jobs=10, concurrency=4)
    assert stats.sent == 8 and stats.failed == 0 and stats.blocked == 0

    for tenant_id, own_leads in cohorts.items():
        with tenant_session(tenant_id) as session:
            states = dict(session.execute(select(Lead.id, Lead.state)).all())
            assert set(states) == set(own_leads)
            assert all(s == "sent" for s in states.values())


def test_attest_sender_identity_endpoint(client):
    onboarded = client.post(
        "/internal/tenants/onboard",
        headers=ADMIN,
        json={
            "name": f"attest-{uuid.uuid4().hex[:8]}",
            "source": {
                "name": "seed",
                "source_type": "seed",
                "terms_allow_use": "yes",
            },
            "campaign": {"name": "c1"},
            "sender_from_address": "client-a@example.test",
        },
    ).json()
    assert onboarded["sender_identity_verified"] is False

    response = client.post(
        f"/internal/tenants/{onboarded['tenant_id']}/attest-sender-identity",
        headers=ADMIN,
    )
    assert response.status_code == 200, response.text
    assert response.json()["sender_identity_verified"] is True

    # No address to attest -> 409; unknown tenant -> 404.
    bare = client.post(
        "/tenants",
        headers=ADMIN,
        json={"name": f"bare-{uuid.uuid4().hex[:8]}"},
    ).json()
    assert (
        client.post(
            f"/internal/tenants/{bare['id']}/attest-sender-identity",
            headers=ADMIN,
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/internal/tenants/{uuid.uuid4()}/attest-sender-identity",
            headers=ADMIN,
        ).status_code
        == 404
    )


def test_worker_pass_budget_is_global_across_tenants(
    tenant_a, tenant_b, factory_a, factory_b
):
    """max_jobs bounds the WHOLE pass, not each tenant: 2 tenants with 2
    queued jobs each and max_jobs=3 process exactly 3, leaving 1 queued."""
    ta, _ = tenant_a
    tb, _ = tenant_b
    for tenant_id, factory in ((ta, factory_a), (tb, factory_b)):
        for _ in range(2):
            lead_id = factory.lead()
            run_to_approval(tenant_id, lead_id)
            approve_current_draft(tenant_id, lead_id)
            outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
            assert outcome.stopped_on == "waiting_worker", outcome

    stats = process_pending(max_jobs=3, concurrency=2)
    assert stats.processed == 3

    remaining = 0
    for tenant_id in (ta, tb):
        with tenant_session(tenant_id) as session:
            remaining += len(
                session.execute(select(SendJob).where(SendJob.status == "queued"))
                .scalars()
                .all()
            )
    assert remaining == 1
