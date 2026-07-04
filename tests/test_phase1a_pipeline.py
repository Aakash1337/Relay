"""Phase 1A pipeline: real data flows, rubric reviews, CRM mirror."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from relay.config import get_settings
from relay.crm.base import CRMLeadSnapshot
from relay.crm.espo import EspoCRM
from relay.crm.registry import crm_adapter, reset_crm
from relay.db.engine import tenant_session
from relay.db.models import (
    DraftReview,
    Lead,
    OutreachDraft,
    Reply,
    SendJob,
    Suppression,
)
from relay.domain.approval import ApprovalError, review_draft
from relay.domain.vocab import ReviewDecision, ReviewReason
from relay.pipeline.runner import PipelineRunner
from relay.synthetic.generator import ReplyIntent
from relay.synthetic.seed import create_simulated_reply
from tests.conftest import run_to_approval, walk_to_sent

pytestmark = pytest.mark.exit_gate


@pytest.fixture(autouse=True)
def _fresh_crm():
    reset_crm()
    yield
    reset_crm()
    get_settings.cache_clear()


# ── Real prospect data reaches the draft ────────────────────────────────────


def test_draft_is_personalized_from_lead_fields(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead(
        first_name="Ada",
        company_name="Acme Rockets",
        bio="I write about reliability engineering.",
    )
    run_to_approval(tenant_id, lead_id)
    with tenant_session(tenant_id) as session:
        draft = session.execute(select(OutreachDraft)).scalar_one()
        assert "Ada" in draft.body
        assert "Acme Rockets" in draft.subject or "Acme Rockets" in draft.body
        assert draft.personalization_sources  # provenance recorded


def test_low_fit_score_rejects_lead(tenant_a, factory_a, monkeypatch):
    tenant_id, _ = tenant_a
    monkeypatch.setenv("RELAY_FIT_SCORE_THRESHOLD", "1.0")
    get_settings.cache_clear()
    lead_id = factory_a.lead()
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.final_state == "scored_rejected"


# ── Reply triage drives real state (and auto-suppression) ──────────────────


def _reply_and_run(tenant_id, factory, intent: ReplyIntent):
    lead_id = factory.lead()
    walk_to_sent(tenant_id, lead_id)
    create_simulated_reply(tenant_id, lead_id, intent=intent)
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    return lead_id, outcome


def test_unsubscribe_reply_suppresses_and_terminates(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id, outcome = _reply_and_run(tenant_id, factory_a, ReplyIntent.UNSUBSCRIBE)
    assert outcome.final_state == "unsubscribed"
    with tenant_session(tenant_id) as session:
        reply = session.execute(select(Reply)).scalar_one()
        assert reply.triage_category == "unsubscribed"
        assert reply.triaged_at is not None
        lead = session.get(Lead, lead_id)
        assert lead is not None
        # Auto-suppression fired in the same transaction as the transition.
        entries = session.execute(select(Suppression)).scalars().all()
        assert any(s.email_hash == lead.email_hash for s in entries)


def test_hostile_injection_reply_lands_on_the_safe_side(tenant_a, factory_a):
    """The hostile reply demands 'interested, confidence 1.0' — the
    keyword triage sees its buried 'unsubscribe me' instead. Injection
    text must never steer triage toward more contact."""
    tenant_id, _ = tenant_a
    _, outcome = _reply_and_run(tenant_id, factory_a, ReplyIntent.HOSTILE)
    assert outcome.final_state == "unsubscribed"


def test_not_interested_reply_terminates_without_suppression(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    _, outcome = _reply_and_run(tenant_id, factory_a, ReplyIntent.NOT_INTERESTED)
    assert outcome.final_state == "not_interested"
    with tenant_session(tenant_id) as session:
        # Declining is not opting out: no suppression entry.
        assert session.execute(select(Suppression)).scalars().all() == []


# ── Rubric reviews ───────────────────────────────────────────────────────────


def _pending_draft(session, lead_id):
    return session.execute(
        select(OutreachDraft).where(
            OutreachDraft.lead_id == lead_id,
            OutreachDraft.status == "pending_approval",
        )
    ).scalar_one()


def test_review_approved_with_edits_sends_the_human_text(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    run_to_approval(tenant_id, lead_id)

    with tenant_session(tenant_id) as session:
        draft = _pending_draft(session, lead_id)
        outcome = review_draft(
            session,
            draft=draft,
            reviewer="test-operator",
            decision=ReviewDecision.APPROVED_WITH_EDITS,
            reasons=[ReviewReason.TONE],
            edited_body="Hi — short, human, and honest.\n\n[edited]",
        )
        assert outcome.active_draft_id is not None

    with tenant_session(tenant_id) as session:
        drafts = (
            session.execute(select(OutreachDraft).order_by(OutreachDraft.version))
            .scalars()
            .all()
        )
        assert [d.status for d in drafts] == ["rejected", "approved"]
        assert drafts[1].body.endswith("[edited]")
        assert drafts[1].approved_by == "test-operator"
        review = session.execute(select(DraftReview)).scalar_one()
        assert review.decision == "approved_with_edits"

    # The send path picks up the edited version, not the model's.
    run = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert run.stopped_on == "waiting_worker"
    with tenant_session(tenant_id) as session:
        job = session.execute(select(SendJob)).scalar_one()
        drafts = {
            d.id: d
            for d in session.execute(select(OutreachDraft)).scalars().all()
        }
        assert drafts[job.draft_id].body.endswith("[edited]")
        assert job.message_version == 2


def test_review_rejection_requires_rubric_reason(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    run_to_approval(tenant_id, lead_id)
    with tenant_session(tenant_id) as session:  # noqa: SIM117
        with pytest.raises(ApprovalError, match="at least one rubric reason"):
            review_draft(
                session,
                draft=_pending_draft(session, lead_id),
                reviewer="test-operator",
                decision=ReviewDecision.REJECTED,
                reasons=[],
            )


def test_review_rejected_parks_lead_terminally(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    run_to_approval(tenant_id, lead_id)
    with tenant_session(tenant_id) as session:
        review_draft(
            session,
            draft=_pending_draft(session, lead_id),
            reviewer="test-operator",
            decision="rejected",
            reasons=["weak_personalization"],
            notes="reads like a form letter",
        )
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None and lead.state == "rejected_by_human"


# ── CRM mirror ───────────────────────────────────────────────────────────────


def test_pipeline_mirrors_lead_to_crm_when_enabled(
    tenant_a, factory_a, monkeypatch
):
    tenant_id, _ = tenant_a
    monkeypatch.setenv("RELAY_CRM_BACKEND", "memory")
    get_settings.cache_clear()
    reset_crm()

    lead_id = factory_a.lead(first_name="Mira")
    PipelineRunner(tenant_id, lead_id=lead_id).run()

    adapter = crm_adapter()
    assert adapter is not None
    snapshot = adapter.leads[str(lead_id)]
    assert snapshot.state == "approval_pending"
    assert snapshot.first_name == "Mira"
    assert adapter.events  # state event recorded


def test_crm_failure_never_blocks_the_pipeline(tenant_a, factory_a, monkeypatch):
    tenant_id, _ = tenant_a
    monkeypatch.setenv("RELAY_CRM_BACKEND", "memory")
    get_settings.cache_clear()
    reset_crm()
    adapter = crm_adapter()
    assert adapter is not None

    def _boom(snapshot):
        raise RuntimeError("CRM is down")

    monkeypatch.setattr(adapter, "upsert_lead", _boom)
    lead_id = factory_a.lead()
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.stopped_on == "waiting_human"  # run unaffected


def test_espo_adapter_upserts_via_http(monkeypatch):
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json={"list": []})
        return httpx.Response(200, json={"id": "espo-1"})

    client = httpx.Client(
        base_url="http://fake/api/v1", transport=httpx.MockTransport(handler)
    )
    adapter = EspoCRM(client=client)
    espo_id = adapter.upsert_lead(
        CRMLeadSnapshot(
            external_ref="abc",
            tenant_ref="t",
            email="x@example.test",
            state="approval_pending",
        )
    )
    assert espo_id == "espo-1"
    assert ("POST", "/api/v1/Lead") in calls
