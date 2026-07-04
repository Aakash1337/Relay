"""API surface: tenant auth, the human gate over HTTP, and the absence
of any endpoint that sends."""

from __future__ import annotations

import uuid

from tests.conftest import ADMIN


def _auth(tenant: dict) -> dict:
    return {"X-API-Key": tenant["api_key"]}


def _setup_chain(client, tenant) -> tuple[str, str]:
    source = client.post(
        "/sources",
        json={
            "name": f"synthetic-{uuid.uuid4().hex[:6]}",
            "source_type": "synthetic",
            "terms_allow_use": "yes",
        },
        headers=_auth(tenant),
    ).json()
    campaign = client.post(
        "/campaigns",
        json={
            "name": f"campaign-{uuid.uuid4().hex[:6]}",
            "dry_run": True,
            "simulated_replies_enabled": True,
        },
        headers=_auth(tenant),
    ).json()
    return source["id"], campaign["id"]


def _create_lead(client, tenant, source_id, campaign_id, email=None) -> dict:
    response = client.post(
        "/leads",
        json={
            "campaign_id": campaign_id,
            "source_id": source_id,
            "email": email or f"lead-{uuid.uuid4().hex[:8]}@example.test",
            "lawful_basis": "synthetic",
            "region_assumption": "none-synthetic",
        },
        headers=_auth(tenant),
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_tenant_bootstrap_requires_admin_token(client):
    assert (
        client.post("/tenants", json={"name": "x"}).status_code
        in (403, 422)  # missing header
    )
    assert (
        client.post(
            "/tenants",
            json={"name": "x"},
            headers={"X-Admin-Token": "wrong"},
        ).status_code
        == 403
    )


def test_invalid_api_key_rejected(client):
    response = client.get(f"/leads/{uuid.uuid4()}", headers={"X-API-Key": "rk_bogus"})
    assert response.status_code == 401


def test_full_journey_over_http(client, api_tenant):
    source_id, campaign_id = _setup_chain(client, api_tenant)
    # Fixed email whose hash-derived reply persona is 'interested' — the
    # journey asserts the booking branch, so the persona must cooperate.
    lead = _create_lead(
        client, api_tenant, source_id, campaign_id, email="journey-3@example.test"
    )
    auth = _auth(api_tenant)

    # Run to the human gate.
    run1 = client.post(f"/leads/{lead['id']}/pipeline/run", json={}, headers=auth)
    assert run1.status_code == 200, run1.text
    assert run1.json()["stopped_on"] == "waiting_human"

    # Find the pending draft via trace + approve it. Approval ≠ send.
    trace = client.get(f"/leads/{lead['id']}/trace", headers=auth).json()
    assert trace["state"] == "approval_pending"

    from sqlalchemy import select

    from relay.db.engine import tenant_session
    from relay.db.models import OutreachDraft

    with tenant_session(uuid.UUID(api_tenant["id"])) as session:
        draft_id = session.execute(select(OutreachDraft.id)).scalar_one()

    approval = client.post(
        f"/outreach-drafts/{draft_id}/approve",
        json={"approver": "api-reviewer"},
        headers=auth,
    )
    assert approval.status_code == 200, approval.text
    body = approval.json()
    assert body["approved"] is True
    assert body["sent"] is False  # the contract, in the response itself
    assert body["lead_state"] == "approved"

    # Continue: eligibility → queued; the spine's tick executes the send.
    run2 = client.post(f"/leads/{lead['id']}/pipeline/run", json={}, headers=auth)
    assert run2.json()["stopped_on"] == "waiting_worker"

    tick = client.post("/internal/send-worker/tick", headers=ADMIN)
    assert tick.status_code == 200
    assert tick.json()["sent"] == 1

    # Finish the journey and check campaign status.
    run3 = client.post(f"/leads/{lead['id']}/pipeline/run", json={}, headers=auth)
    assert run3.json()["final_state"] == "closed"

    status = client.get(f"/campaigns/{campaign_id}/status", headers=auth).json()
    assert status["lead_states"] == {"closed": 1}
    assert status["send_jobs"] == {"sent": 1}


def test_worker_tick_requires_admin_not_tenant_key(client, api_tenant):
    response = client.post("/internal/send-worker/tick", headers=_auth(api_tenant))
    assert response.status_code in (403, 422)


def test_cross_tenant_lead_invisible_over_http(client, api_tenant):
    source_id, campaign_id = _setup_chain(client, api_tenant)
    lead = _create_lead(client, api_tenant, source_id, campaign_id)

    other = client.post(
        "/tenants",
        json={"name": f"other-{uuid.uuid4().hex[:8]}"},
        headers=ADMIN,
    ).json()
    response = client.get(f"/leads/{lead['id']}", headers=_auth(other))
    assert response.status_code == 404


def test_duplicate_lead_conflict(client, api_tenant):
    source_id, campaign_id = _setup_chain(client, api_tenant)
    payload = {
        "campaign_id": campaign_id,
        "source_id": source_id,
        "email": "dupe-api@example.test",
        "lawful_basis": "synthetic",
        "region_assumption": "none-synthetic",
    }
    auth = _auth(api_tenant)
    assert client.post("/leads", json=payload, headers=auth).status_code == 201
    assert client.post("/leads", json=payload, headers=auth).status_code == 409


def test_guardrail_kill_surfaces_as_conflict(client, api_tenant):
    source_id, campaign_id = _setup_chain(client, api_tenant)
    lead = _create_lead(client, api_tenant, source_id, campaign_id)
    response = client.post(
        f"/leads/{lead['id']}/pipeline/run",
        json={"max_iterations": 2},
        headers=_auth(api_tenant),
    )
    assert response.status_code == 409
    assert "guardrail" in response.json()["detail"]


def test_illegal_reject_returns_409_not_500(client, api_tenant):
    """A draft is pending_approval but its lead was left at draft_ready by a
    guardrail kill: rejecting it is an illegal lead transition and must
    surface as a 409, not an unhandled 500."""
    source_id, campaign_id = _setup_chain(client, api_tenant)
    lead = _create_lead(client, api_tenant, source_id, campaign_id)
    auth = _auth(api_tenant)
    # max_iterations=9 kills just after the draft becomes ready (the lead
    # stays at draft_ready with a pending_approval draft).
    killed = client.post(
        f"/leads/{lead['id']}/pipeline/run",
        json={"max_iterations": 9},
        headers=auth,
    )
    assert killed.status_code == 409

    from sqlalchemy import select

    from relay.db.engine import tenant_session
    from relay.db.models import Lead, OutreachDraft

    with tenant_session(uuid.UUID(api_tenant["id"])) as session:
        draft = session.execute(select(OutreachDraft)).scalar_one()
        lead_row = session.get(Lead, uuid.UUID(lead["id"]))
        assert draft.status == "pending_approval"
        assert lead_row is not None and lead_row.state == "draft_ready"
        draft_id = draft.id

    rejected = client.post(
        f"/outreach-drafts/{draft_id}/reject",
        json={"approver": "reviewer", "reason": "changed my mind"},
        headers=auth,
    )
    assert rejected.status_code == 409  # not a 500
