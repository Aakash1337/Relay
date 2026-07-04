"""Phase 1A API surface: review queue, rubric endpoint, economics, UI page."""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import ADMIN
from tests.test_api import _auth, _create_lead, _setup_chain

pytestmark = pytest.mark.exit_gate


def _to_gate(client, tenant, email=None):
    source_id, campaign_id = _setup_chain(client, tenant)
    lead = _create_lead(client, tenant, source_id, campaign_id, email=email)
    run = client.post(
        f"/leads/{lead['id']}/pipeline/run", json={}, headers=_auth(tenant)
    )
    assert run.json()["stopped_on"] == "waiting_human"
    return campaign_id, lead


def test_pending_queue_lists_the_draft(client, api_tenant):
    _, lead = _to_gate(client, api_tenant)
    res = client.get("/outreach-drafts/pending", headers=_auth(api_tenant))
    assert res.status_code == 200
    drafts = res.json()["drafts"]
    assert len(drafts) == 1
    assert drafts[0]["lead_id"] == lead["id"]
    assert drafts[0]["subject"]


def test_review_endpoint_approves_with_edits(client, api_tenant):
    _, lead = _to_gate(client, api_tenant)
    auth = _auth(api_tenant)
    draft_id = client.get("/outreach-drafts/pending", headers=auth).json()["drafts"][0][
        "draft_id"
    ]

    res = client.post(
        f"/outreach-drafts/{draft_id}/review",
        json={
            "reviewer": "ui-reviewer",
            "decision": "approved_with_edits",
            "reasons": ["tone"],
            "edited_body": "Short and human. [edited]",
        },
        headers=auth,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["sent"] is False  # the contract, in the response
    assert body["active_draft_id"] != str(draft_id)
    assert body["lead_state"] == "approved"
    # Queue is empty afterwards.
    assert client.get("/outreach-drafts/pending", headers=auth).json()["drafts"] == []


def test_review_endpoint_rejects_bad_rubric(client, api_tenant):
    _, lead = _to_gate(client, api_tenant)
    auth = _auth(api_tenant)
    draft_id = client.get("/outreach-drafts/pending", headers=auth).json()["drafts"][0][
        "draft_id"
    ]
    # Rejection without a reason: schema-valid, rubric-invalid → 409.
    res = client.post(
        f"/outreach-drafts/{draft_id}/review",
        json={"reviewer": "r", "decision": "rejected", "reasons": []},
        headers=auth,
    )
    assert res.status_code == 409
    # Unknown reason value: schema-invalid → 422.
    res = client.post(
        f"/outreach-drafts/{draft_id}/review",
        json={"reviewer": "r", "decision": "rejected", "reasons": ["vibes"]},
        headers=auth,
    )
    assert res.status_code == 422


def test_economics_reflects_the_funnel(client, api_tenant):
    campaign_id, lead = _to_gate(client, api_tenant, email="journey-3@example.test")
    auth = _auth(api_tenant)
    draft_id = client.get("/outreach-drafts/pending", headers=auth).json()["drafts"][0][
        "draft_id"
    ]
    client.post(
        f"/outreach-drafts/{draft_id}/review",
        json={"reviewer": "r", "decision": "approved", "reasons": []},
        headers=auth,
    )
    client.post(f"/leads/{lead['id']}/pipeline/run", json={}, headers=auth)
    client.post("/internal/send-worker/tick", headers=ADMIN)
    client.post(f"/leads/{lead['id']}/pipeline/run", json={}, headers=auth)

    res = client.get(f"/campaigns/{campaign_id}/economics", headers=auth)
    assert res.status_code == 200
    data = res.json()
    funnel = data["funnel"]
    assert funnel["leads"] == 1
    assert funnel["qualified"] == 1
    assert funnel["reviewed"] == 1
    assert funnel["sent"] == 1
    assert funnel["booked"] == 1
    assert data["cost_units_total"] > 0
    assert data["cost_units_per_meeting"] == data["cost_units_total"]
    assert data["cost_usd_per_meeting"] is None  # rate not calibrated


def test_economics_404_for_foreign_campaign(client, api_tenant):
    res = client.get(f"/campaigns/{uuid.uuid4()}/economics", headers=_auth(api_tenant))
    assert res.status_code == 404


def test_review_page_is_served_and_self_contained(client):
    res = client.get("/review")
    assert res.status_code == 200
    html = res.text
    assert "RELAY review queue" in html
    assert "never sends" in html
    # Self-contained: no external scripts, styles, or fonts.
    assert "https://" not in html and "http://" not in html
    assert "<script src" not in html and "link rel" not in html
