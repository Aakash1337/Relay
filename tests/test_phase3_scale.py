"""Phase 3 production readiness: human-in-the-loop at scale, secrets
rotation, and reputation monitoring."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from relay.config import get_settings
from relay.db.engine import tenant_session
from relay.db.models import Lead
from relay.domain.suppression import add_suppression
from relay.ingest.unsubscribe import (
    UnsubscribeRejected,
    build_token,
    verify_token,
)
from relay.observability import evaluate_alerts, tenant_metrics
from tests.conftest import ADMIN, run_to_approval, walk_to_sent

pytestmark = pytest.mark.exit_gate


# ── Batched review: human-in-the-loop at scale ──────────────────────────────


def _pending_draft_id(tenant_id, lead_id) -> uuid.UUID:
    from relay.db.models import OutreachDraft

    with tenant_session(tenant_id) as session:
        return session.execute(
            select(OutreachDraft.id).where(
                OutreachDraft.lead_id == lead_id,
                OutreachDraft.status == "pending_approval",
            )
        ).scalar_one()


def test_batch_review_processes_items_independently(client, tenant_a, factory_a):
    """One batch call: an approval, a rejection, and a bogus draft id.
    The bad item fails alone; the good ones land; nothing sends."""
    tenant_id, api_key = tenant_a
    leads = [factory_a.lead() for _ in range(2)]
    for lead_id in leads:
        run_to_approval(tenant_id, lead_id)
    approve_id = _pending_draft_id(tenant_id, leads[0])
    reject_id = _pending_draft_id(tenant_id, leads[1])

    response = client.post(
        "/outreach-drafts/batch-review",
        headers={"X-API-Key": api_key},
        json={
            "reviewer": "batch-reviewer",
            "items": [
                {"draft_id": str(approve_id), "decision": "approved"},
                {
                    "draft_id": str(reject_id),
                    "decision": "rejected",
                    "reasons": ["tone"],
                },
                {"draft_id": str(uuid.uuid4()), "decision": "approved"},
            ],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["sent"] is False
    assert (body["approved"], body["rejected"], body["failed"]) == (1, 1, 1)
    ok_by_id = {r["draft_id"]: r["ok"] for r in body["results"]}
    assert ok_by_id[str(approve_id)] and ok_by_id[str(reject_id)]

    with tenant_session(tenant_id) as session:
        states = {str(lead_id): session.get(Lead, lead_id).state for lead_id in leads}
    assert states[str(leads[0])] == "approved"
    assert states[str(leads[1])] == "rejected_by_human"


def test_review_queue_is_confidence_ordered(client, tenant_a, factory_a):
    """The queue surfaces the highest-confidence drafts first and carries
    fit_score so a reviewer can split batch-tail from careful-review."""
    tenant_id, api_key = tenant_a
    leads = [factory_a.lead() for _ in range(3)]
    for lead_id in leads:
        run_to_approval(tenant_id, lead_id)
    scores = {leads[0]: 0.31, leads[1]: 0.97, leads[2]: 0.55}
    with tenant_session(tenant_id) as session:
        for lead_id, score in scores.items():
            session.get(Lead, lead_id).fit_score = score

    response = client.get("/outreach-drafts/pending", headers={"X-API-Key": api_key})
    assert response.status_code == 200
    listed = [
        (d["lead_id"], d["fit_score"])
        for d in response.json()["drafts"]
        if uuid.UUID(d["lead_id"]) in scores
    ]
    assert [s for _, s in listed] == sorted((s for s in scores.values()), reverse=True)


# ── Secrets rotation ────────────────────────────────────────────────────────


def test_tenant_api_key_rotation_invalidates_old_key(client, api_tenant):
    old_key = api_tenant["api_key"]
    assert (
        client.get(
            "/outreach-drafts/pending", headers={"X-API-Key": old_key}
        ).status_code
        == 200
    )

    response = client.post(
        f"/internal/tenants/{api_tenant['id']}/rotate-key", headers=ADMIN
    )
    assert response.status_code == 200, response.text
    new_key = response.json()["api_key"]
    assert new_key != old_key

    # The old key dies immediately; the new one works.
    assert (
        client.get(
            "/outreach-drafts/pending", headers={"X-API-Key": old_key}
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/outreach-drafts/pending", headers={"X-API-Key": new_key}
        ).status_code
        == 200
    )


def test_rotate_key_requires_admin_and_known_tenant(client, api_tenant):
    url = f"/internal/tenants/{api_tenant['id']}/rotate-key"
    assert client.post(url).status_code == 422  # header missing entirely
    assert client.post(url, headers={"X-Admin-Token": "wrong-token"}).status_code == 403
    assert (
        client.post(
            f"/internal/tenants/{uuid.uuid4()}/rotate-key", headers=ADMIN
        ).status_code
        == 404
    )


def test_master_key_rotation_keeps_old_unsubscribe_tokens_alive(tenant_a, monkeypatch):
    """Unsubscribe links already sitting in delivered mail MUST keep
    working across a master-key rotation (a dead unsubscribe link is a
    compliance failure) — via RELAY_MASTER_KEY_PREVIOUS, verify-only."""
    tenant_id, _ = tenant_a
    lead_id, job_id = uuid.uuid4(), uuid.uuid4()
    old_token = build_token(tenant_id, lead_id, job_id)

    monkeypatch.setenv("RELAY_MASTER_KEY", "rotated-master-key")
    get_settings.cache_clear()
    # Without the previous key configured, the old link would die…
    with pytest.raises(UnsubscribeRejected):
        verify_token(old_token)

    monkeypatch.setenv("RELAY_MASTER_KEY_PREVIOUS", "dev-master-key-not-for-production")
    get_settings.cache_clear()
    # …with it, the old link verifies AND new tokens use the new key.
    assert verify_token(old_token) == (tenant_id, lead_id, job_id)
    new_token = build_token(tenant_id, lead_id, job_id)
    assert new_token != old_token
    assert verify_token(new_token) == (tenant_id, lead_id, job_id)

    monkeypatch.delenv("RELAY_MASTER_KEY_PREVIOUS")
    monkeypatch.delenv("RELAY_MASTER_KEY")
    get_settings.cache_clear()


# ── Reputation monitoring ───────────────────────────────────────────────────


def test_metrics_expose_reputation_and_edit_signal(client, tenant_a, factory_a):
    tenant_id, api_key = tenant_a
    sent_lead = factory_a.lead()
    walk_to_sent(tenant_id, sent_lead)
    with tenant_session(tenant_id) as session:
        add_suppression(
            session,
            tenant_id=tenant_id,
            reason="hard_bounce",
            source="provider_webhook",
            created_by="test",
            email=f"dead-{uuid.uuid4().hex[:6]}@example.test",
        )

    # An edits-as-signal data point: review a pending draft with edits.
    edit_lead = factory_a.lead()
    run_to_approval(tenant_id, edit_lead)
    draft_id = _pending_draft_id(tenant_id, edit_lead)
    response = client.post(
        f"/outreach-drafts/{draft_id}/review",
        headers={"X-API-Key": api_key},
        json={
            "reviewer": "editor",
            "decision": "approved_with_edits",
            "reasons": ["tone"],
            "edited_body": "Hand-tuned body.",
        },
    )
    assert response.status_code == 200, response.text

    m = tenant_metrics(tenant_id)
    assert m.suppressions_window.get("hard_bounce", 0) >= 1
    assert m.bounce_rate is not None and m.bounce_rate > 0
    assert m.reviews_window.get("approved_with_edits", 0) >= 1
    assert m.edit_rate is not None and m.edit_rate > 0

    body = client.get("/metrics", headers={"X-API-Key": api_key}).json()
    assert body["bounce_rate"] == pytest.approx(m.bounce_rate)
    assert body["edit_rate"] == pytest.approx(m.edit_rate)
    prom = client.get("/metrics/prometheus", headers={"X-API-Key": api_key}).text
    assert "relay_suppressions_window{" in prom
    assert "relay_reviews_window{" in prom


def test_bounce_rate_alert_fires_past_threshold(tenant_a, factory_a, monkeypatch):
    """One bounce over one send is 100% — over any sane threshold — but
    the rule stays quiet until min_sends is met, then fires."""
    tenant_id, _ = tenant_a
    monkeypatch.setenv("RELAY_ALERT_BOUNCE_RATE", "0.05")
    monkeypatch.setenv("RELAY_ALERT_BOUNCE_RATE_MIN_SENDS", "2")
    get_settings.cache_clear()

    walk_to_sent(tenant_id, factory_a.lead())
    with tenant_session(tenant_id) as session:
        add_suppression(
            session,
            tenant_id=tenant_id,
            reason="hard_bounce",
            source="provider_webhook",
            created_by="test",
            email=f"dead-{uuid.uuid4().hex[:6]}@example.test",
        )
    # 1 send < min_sends: noise, not reputation — no alert.
    assert not any(a.rule == "bounce_rate_high" for a in evaluate_alerts(tenant_id))

    walk_to_sent(tenant_id, factory_a.lead())
    fired = [a for a in evaluate_alerts(tenant_id) if a.rule == "bounce_rate_high"]
    assert len(fired) == 1 and fired[0].severity == "critical"
    assert fired[0].value == pytest.approx(0.5)
