"""Phase 1C: the real send path — SES sandbox pilot, hermetic.

Exit-gate shape: a handful of real, eligible, approved, non-duplicate
sends go out through the approved provider — proven here against a fake
SES client; the live smoke reuses exactly this path with credentials.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from relay.config import get_settings
from relay.db.engine import tenant_session
from relay.db.models import Lead, SendJob
from relay.senders import RealSendUnavailable, reset_senders, sender_for_mode
from relay.senders.registry import _cache as _sender_cache
from relay.senders.ses import SESSender
from relay.workers.send_worker import process_pending
from tests.conftest import approve_current_draft, run_to_approval

pytestmark = pytest.mark.exit_gate

_PILOT_ENV = {
    "RELAY_REAL_SEND_ENABLED": "true",
    "RELAY_SENDER_PROVIDER": "ses",
    "RELAY_SES_FROM_ADDRESS": "pilot@testings.work",
    "RELAY_SENDER_IDENTITY_APPROVED": "true",
    "RELAY_SENDER_DOMAIN_AUTHENTICATED": "true",
    "RELAY_UNSUBSCRIBE_MAILTO": "unsubscribe@testings.work",
    "RELAY_PROVIDER_TERMS_RECORD": "docs/decisions/sending-provider.md",
}


class FakeSES:
    """Stands in for the boto3 SESv2 client; records every request."""

    def __init__(self) -> None:
        self.requests: list[dict] = []

    def send_email(self, **kwargs):
        self.requests.append(kwargs)
        return {"MessageId": f"ses-fake-{len(self.requests)}"}


@pytest.fixture
def pilot_env(monkeypatch):
    for key, value in _PILOT_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    reset_senders()
    fake = FakeSES()
    _sender_cache["ses"] = SESSender(client=fake)
    yield fake
    reset_senders()
    get_settings.cache_clear()


def _pilot_lead(factory, **overrides) -> uuid.UUID:
    """A dry_run=False chain to a test_consent inbox (§6: self-to-self)."""
    campaign_id = overrides.pop(
        "campaign_id", factory.campaign(dry_run=False, simulated_replies=False)
    )
    defaults = {
        "campaign_id": campaign_id,
        "dry_run": False,
        "lawful_basis": "test_consent",
        "email": f"ourbox-{uuid.uuid4().hex[:6]}@example.test",
    }
    defaults.update(overrides)
    return factory.lead(**defaults)


def _walk_to_queue(tenant_id, lead_id) -> None:
    from relay.pipeline.runner import PipelineRunner

    run_to_approval(tenant_id, lead_id)
    approve_current_draft(tenant_id, lead_id)
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.stopped_on == "waiting_worker", outcome


# ── The seam itself ──────────────────────────────────────────────────────────


def test_simulated_mode_never_gets_a_real_provider(pilot_env):
    """Even fully configured for SES, simulated jobs use the simulated
    sender — that pairing is not configurable, by design."""
    assert sender_for_mode("simulated").name == "simulated"


def test_ses_sender_requires_configuration(monkeypatch):
    monkeypatch.setenv("RELAY_SES_FROM_ADDRESS", "")
    get_settings.cache_clear()
    with pytest.raises(RealSendUnavailable, match="RELAY_SES_FROM_ADDRESS"):
        SESSender(client=FakeSES())
    get_settings.cache_clear()


# ── The pilot: real, eligible, approved, non-duplicate sends ───────────────


def test_real_pilot_send_goes_through_ses(tenant_a, factory_a, pilot_env):
    tenant_id, _ = tenant_a
    lead_id = _pilot_lead(factory_a)
    _walk_to_queue(tenant_id, lead_id)

    with tenant_session(tenant_id) as session:
        job = session.execute(select(SendJob)).scalar_one()
        assert job.mode == "real"  # not silently simulated

    stats = process_pending()
    assert stats.sent == 1

    (request,) = pilot_env.requests
    assert request["FromEmailAddress"] == "pilot@testings.work"
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        job = session.execute(select(SendJob)).scalar_one()
        assert request["Destination"]["ToAddresses"] == [lead.email]
        assert job.status == "sent"
        assert job.provider_message_id == "ses-fake-1"
        assert lead.state == "sent"
    headers = request["Content"]["Simple"]["Headers"]
    assert any(
        h["Name"] == "List-Unsubscribe" and "unsubscribe@testings.work" in h["Value"]
        for h in headers
    )
    assert any(h["Name"] == "List-Unsubscribe-Post" for h in headers)


@pytest.mark.parametrize(
    ("missing", "failing_check"),
    [
        ("RELAY_REAL_SEND_ENABLED", "real_send_enabled"),
        ("RELAY_SENDER_IDENTITY_APPROVED", "sender_identity_approved"),
        ("RELAY_SENDER_DOMAIN_AUTHENTICATED", "domain_authenticated"),
        ("RELAY_UNSUBSCRIBE_MAILTO", "unsubscribe_mechanism_present"),
        ("RELAY_PROVIDER_TERMS_RECORD", "provider_terms_allow"),
    ],
)
def test_each_missing_attest_blocks_the_send(
    tenant_a, factory_a, pilot_env, monkeypatch, missing, failing_check
):
    """The real-mode checklist is a conjunction: remove any single attest
    and the lead lands in send_blocked naming that exact check."""
    tenant_id, _ = tenant_a
    off = "" if missing.endswith(("MAILTO", "RECORD")) else "false"
    monkeypatch.setenv(missing, off)
    get_settings.cache_clear()

    lead_id = _pilot_lead(factory_a)
    run_to_approval(tenant_id, lead_id)
    approve_current_draft(tenant_id, lead_id)
    from relay.pipeline.runner import PipelineRunner

    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.final_state == "send_blocked"
    with tenant_session(tenant_id) as session:
        from relay.db.models import LeadTransition

        reason = session.execute(
            select(LeadTransition.reason).where(
                LeadTransition.lead_id == lead_id,
                LeadTransition.to_state == "send_blocked",
            )
        ).scalar_one()
        assert failing_check in (reason or "")


def test_daily_cap_blocks_the_next_real_send(
    tenant_a, factory_a, pilot_env, monkeypatch
):
    tenant_id, _ = tenant_a
    monkeypatch.setenv("RELAY_REAL_SEND_DAILY_CAP", "1")
    get_settings.cache_clear()

    first = _pilot_lead(factory_a)
    _walk_to_queue(tenant_id, first)
    assert process_pending().sent == 1

    second = _pilot_lead(factory_a)
    run_to_approval(tenant_id, second)
    approve_current_draft(tenant_id, second)
    from relay.pipeline.runner import PipelineRunner

    outcome = PipelineRunner(tenant_id, lead_id=second).run()
    assert outcome.final_state == "send_blocked"
    assert len(pilot_env.requests) == 1  # the cap held before the provider


def test_real_send_requires_test_consent_basis(tenant_a, factory_a, pilot_env):
    """§6 pilot rule, code layer: a synthetic (fake-person) lead cannot
    produce real email even with every attest in place."""
    tenant_id, _ = tenant_a
    lead_id = _pilot_lead(factory_a, lawful_basis="synthetic")
    run_to_approval(tenant_id, lead_id)
    approve_current_draft(tenant_id, lead_id)
    from relay.pipeline.runner import PipelineRunner

    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.final_state == "send_blocked"
    assert pilot_env.requests == []


def test_trigger_backstop_rejects_real_job_for_non_test_consent(
    tenant_a, factory_a, pilot_env
):
    """§6 pilot rule, DB layer: raw SQL cannot queue a real-mode job for
    anything but a test_consent inbox."""
    tenant_id, _ = tenant_a
    lead_id = _pilot_lead(factory_a, lawful_basis="synthetic")
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        params = {
            "tenant": str(tenant_id),
            "campaign": str(lead.campaign_id),
            "lead": str(lead_id),
            "draft": str(uuid.uuid4()),
            "key": f"raw-{uuid.uuid4().hex}",
            "hash": lead.email_hash,
            "domain": lead.email_domain,
        }
    with pytest.raises(IntegrityError, match="test_consent"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.execute(
                text(
                    "INSERT INTO send_jobs (tenant_id, campaign_id, lead_id,"
                    " draft_id, sequence_step, message_version,"
                    " idempotency_key, mode, recipient_email_hash,"
                    " recipient_domain) VALUES (:tenant, :campaign, :lead,"
                    " :draft, 1, 1, :key, 'real', :hash, :domain)"
                ),
                params,
            )


def test_ses_sender_refuses_recipient_hash_mismatch(tenant_a, factory_a, pilot_env):
    """Last-hop cross-check: a lead whose address no longer matches the
    job's frozen hash is refused at the provider boundary too."""
    tenant_id, _ = tenant_a
    lead_id = _pilot_lead(factory_a)
    _walk_to_queue(tenant_id, lead_id)
    with tenant_session(tenant_id) as session:
        job = session.execute(select(SendJob)).scalar_one()
        lead = session.get(Lead, lead_id)
        columns = {c.name: getattr(lead, c.name) for c in Lead.__table__.columns}
        tampered = Lead(**columns)
        tampered.email = "other@attacker.test"
        sender = sender_for_mode("real")
        with pytest.raises(RealSendUnavailable, match="frozen recipient hash"):
            sender.send(job=job, draft=None, lead=tampered)  # type: ignore[arg-type]
    assert pilot_env.requests == []


def test_bounce_complaint_threshold_pauses_sending(
    tenant_a, factory_a, pilot_env, monkeypatch
):
    """Reputation guard: once the window holds too many bounces or
    complaints, further real sends are blocked automatically."""
    tenant_id, _ = tenant_a
    monkeypatch.setenv("RELAY_MAX_BOUNCES_COMPLAINTS_IN_WINDOW", "1")
    get_settings.cache_clear()

    from relay.domain.suppression import add_suppression

    with tenant_session(tenant_id) as session:
        add_suppression(
            session,
            tenant_id=tenant_id,
            reason="hard_bounce",
            source="provider_webhook",
            created_by="test",
            email="bounced@example.test",
        )

    lead_id = _pilot_lead(factory_a)
    run_to_approval(tenant_id, lead_id)
    approve_current_draft(tenant_id, lead_id)
    from relay.pipeline.runner import PipelineRunner

    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.final_state == "send_blocked"
    with tenant_session(tenant_id) as session:
        from relay.db.models import LeadTransition

        reason = session.execute(
            select(LeadTransition.reason).where(
                LeadTransition.lead_id == lead_id,
                LeadTransition.to_state == "send_blocked",
            )
        ).scalar_one()
        assert "campaign_below_thresholds" in (reason or "")
