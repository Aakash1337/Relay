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

#: The one verified pilot inbox — fixed so it can sit on the allowlist.
_PILOT_INBOX = "pilot-inbox@example.test"

_PILOT_ENV = {
    "RELAY_REAL_SEND_ENABLED": "true",
    "RELAY_SENDER_PROVIDER": "ses",
    # Canonical operator-facing names (aliases): AWS_REGION is boto3's own
    # var; RELAY_SES_FROM is the from address; RELAY_PILOT_RECIPIENTS is the
    # allowlist. That these drive the gate proves the aliases are wired.
    "AWS_REGION": "us-east-2",
    "RELAY_SES_FROM": "pilot@testings.work",
    "RELAY_PILOT_RECIPIENTS": _PILOT_INBOX,
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
        "email": _PILOT_INBOX,  # on the allowlist by default
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
    monkeypatch.setenv("RELAY_SES_FROM", "")
    get_settings.cache_clear()
    with pytest.raises(RealSendUnavailable, match="RELAY_SES_FROM"):
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
    # Mailto-only config: One-Click must NOT be advertised (RFC 8058 needs
    # an https endpoint; a mailto cannot honor a one-click POST).
    assert not any(h["Name"] == "List-Unsubscribe-Post" for h in headers)


def test_one_click_only_with_https_url(tenant_a, factory_a, pilot_env, monkeypatch):
    tenant_id, _ = tenant_a
    monkeypatch.setenv("RELAY_UNSUBSCRIBE_URL", "https://relay.example/u/abc")
    get_settings.cache_clear()
    reset_senders()
    from relay.senders.ses import SESSender

    _sender_cache["ses"] = SESSender(client=pilot_env)
    lead_id = _pilot_lead(factory_a)
    _walk_to_queue(tenant_id, lead_id)
    process_pending()
    headers = pilot_env.requests[0]["Content"]["Simple"]["Headers"]
    lu = next(h["Value"] for h in headers if h["Name"] == "List-Unsubscribe")
    assert "https://relay.example/u/abc" in lu and "mailto:" in lu
    assert any(
        h["Name"] == "List-Unsubscribe-Post"
        and h["Value"] == "List-Unsubscribe=One-Click"
        for h in headers
    )


@pytest.mark.parametrize(
    ("missing", "failing_check"),
    [
        ("RELAY_REAL_SEND_ENABLED", "real_send_enabled"),
        ("RELAY_SENDER_IDENTITY_APPROVED", "sender_identity_approved"),
        ("RELAY_SENDER_DOMAIN_AUTHENTICATED", "domain_authenticated"),
        ("RELAY_UNSUBSCRIBE_MAILTO", "unsubscribe_mechanism_present"),
        ("RELAY_PROVIDER_TERMS_RECORD", "provider_terms_allow"),
        # Provider-neutral readiness: a half-configured SES (no region)
        # must be caught at the gate, not stranded in error_terminal later.
        ("AWS_REGION", "sender_configured"),
        # Fail-closed allowlist: an empty RELAY_PILOT_RECIPIENTS blocks
        # every real send.
        ("RELAY_PILOT_RECIPIENTS", "recipient_on_pilot_allowlist"),
    ],
)
def test_each_missing_attest_blocks_the_send(
    tenant_a, factory_a, pilot_env, monkeypatch, missing, failing_check
):
    """The real-mode checklist is a conjunction: remove any single attest
    and the lead lands in send_blocked naming that exact check."""
    tenant_id, _ = tenant_a
    off = (
        ""
        if missing.endswith(("MAILTO", "RECORD", "REGION", "RECIPIENTS"))
        else "false"
    )
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


def test_provider_exception_marks_job_failed_never_sent(tenant_a, factory_a, pilot_env):
    """The live-path failure mode the fake client hides: when the provider
    raises mid-send, the worker must fail the job and park the lead in
    error_terminal — NEVER mark it sent. Drives the real except-handler in
    process_pending, not _mark_failed directly."""
    tenant_id, _ = tenant_a
    lead_id = _pilot_lead(factory_a)
    _walk_to_queue(tenant_id, lead_id)

    def boom(**kwargs):
        raise RuntimeError("SES Throttling: rate exceeded")

    pilot_env.send_email = boom  # the real boto3 client raises ClientError
    stats = process_pending()

    assert stats.sent == 0 and stats.failed == 1
    with tenant_session(tenant_id) as session:
        job = session.execute(select(SendJob)).scalar_one()
        lead = session.get(Lead, lead_id)
        assert job.status == "failed"  # not 'sent'
        assert job.provider_message_id is None
        assert lead is not None and lead.state == "error_terminal"


def test_recipient_off_the_allowlist_is_blocked(tenant_a, factory_a, pilot_env):
    """A test_consent lead whose address is NOT on RELAY_PILOT_RECIPIENTS
    cannot be sent to — the allowlist is a structural gate, not advice."""
    tenant_id, _ = tenant_a
    lead_id = _pilot_lead(factory_a, email="not-my-inbox@example.test")
    run_to_approval(tenant_id, lead_id)
    approve_current_draft(tenant_id, lead_id)
    from relay.pipeline.runner import PipelineRunner

    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.final_state == "send_blocked"
    assert pilot_env.requests == []


def test_real_person_lead_cannot_enter_the_pilot_send_set(
    tenant_a, factory_a, pilot_env
):
    """The design invariant: the Phase 1B draft-only rule holds THROUGH the
    pilot. A real-person (legitimate_interest) lead — even one lawfully
    ingested behind an approved preflight, even listed on the pilot
    allowlist — can never be selected for a real send, in code OR at the
    DB trigger. Only test_consent (our own inboxes) reaches the send set."""
    tenant_id, _ = tenant_a
    from datetime import UTC, datetime, timedelta

    from relay.domain import preflight

    preflight.approve(
        tenant_id,
        artifact_sha256="a" * 64,
        approved_by="compliance",
        artifact_ref="docs/legal-data-preflight.md@test",
    )
    campaign_id = factory_a.campaign(dry_run=False, simulated_replies=False)
    lead_id = factory_a.lead(
        campaign_id=campaign_id,
        dry_run=False,
        lawful_basis="legitimate_interest",
        region_assumption="us-b2b",
        retention_until=datetime.now(tz=UTC) + timedelta(days=90),
        email="real.person@northwind-corp.com",
    )

    # Code layer: the pipeline never queues a send — real-data leads are
    # draft-only, so it stops at the human gate and cannot pass eligibility.
    run_to_approval(tenant_id, lead_id)
    approve_current_draft(tenant_id, lead_id)
    from relay.pipeline.runner import PipelineRunner

    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.final_state == "send_blocked"
    with tenant_session(tenant_id) as session:
        assert session.execute(select(SendJob)).scalars().all() == []
    assert pilot_env.requests == []

    # DB layer: even a raw-SQL real job for this lead is rejected outright.
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
    # Rejected by the DB backstop — for a real-data basis the Phase 1B
    # "send path closed" guard fires first (an even stronger rejection than
    # the 1C test_consent rule); either way no real job can exist.
    with pytest.raises(  # noqa: SIM117
        IntegrityError, match="send path is closed|test_consent"
    ):
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


def test_ses_sender_refuses_recipient_off_allowlist(
    tenant_a, factory_a, pilot_env, monkeypatch
):
    """Last-hop backstop of the §6 allowlist: even a job that passed
    eligibility is refused at the provider boundary when its recipient is
    not on RELAY_PILOT_RECIPIENTS (e.g. the list changed between queue
    and send)."""
    tenant_id, _ = tenant_a
    lead_id = _pilot_lead(factory_a)
    _walk_to_queue(tenant_id, lead_id)

    monkeypatch.setenv("RELAY_PILOT_RECIPIENTS", "someone-else@example.test")
    get_settings.cache_clear()
    fake = FakeSES()
    sender = SESSender(client=fake)  # allowlist frozen at construction
    with tenant_session(tenant_id) as session:
        job = session.execute(select(SendJob)).scalar_one()
        lead = session.get(Lead, lead_id)
        with pytest.raises(RealSendUnavailable, match="pilot allowlist"):
            sender.send(job=job, draft=None, lead=lead)  # type: ignore[arg-type]
    assert fake.requests == []


def test_daily_cap_counts_crash_recovered_unknown_outcome_jobs(
    tenant_a, factory_a, pilot_env, monkeypatch
):
    """A crash-orphaned job ('failed' with started_at set — outcome
    unknown, the mail may have left) must count toward the cap, and the
    window keys on when the send began, not when the job was queued."""
    tenant_id, _ = tenant_a
    monkeypatch.setenv("RELAY_REAL_SEND_DAILY_CAP", "1")
    get_settings.cache_clear()

    from datetime import UTC, datetime

    first = _pilot_lead(factory_a)
    _walk_to_queue(tenant_id, first)
    # Simulate the crash exactly as recovery records it: the claim was
    # committed (sending, started_at set), the process died, recovery
    # marked the job failed — started_at survives.
    with tenant_session(tenant_id) as session:
        job = session.execute(select(SendJob)).scalar_one()
        job.status = "sending"
        job.started_at = datetime.now(tz=UTC)
    with tenant_session(tenant_id) as session:
        job = session.execute(select(SendJob)).scalar_one()
        job.status = "failed"
        job.error = "orphaned mid-send by crash; outcome unknown"

    second = _pilot_lead(factory_a)
    run_to_approval(tenant_id, second)
    approve_current_draft(tenant_id, second)
    from relay.pipeline.runner import PipelineRunner

    outcome = PipelineRunner(tenant_id, lead_id=second).run()
    assert outcome.final_state == "send_blocked"
    assert pilot_env.requests == []
    with tenant_session(tenant_id) as session:
        from relay.db.models import LeadTransition

        reason = session.execute(
            select(LeadTransition.reason).where(
                LeadTransition.lead_id == second,
                LeadTransition.to_state == "send_blocked",
            )
        ).scalar_one()
        assert "mailbox_active_below_cap" in (reason or "")


def test_daily_cap_holds_under_concurrent_workers(
    tenant_a, factory_a, pilot_env, monkeypatch
):
    """Race the cap: with cap=2 and three approved jobs, four concurrent
    workers must send exactly two. Each worker's in-flight claim is
    invisible to the others until commit, so without the per-tenant
    advisory lock every worker would count the same committed total and
    all three sends would go out."""
    tenant_id, _ = tenant_a
    monkeypatch.setenv("RELAY_REAL_SEND_DAILY_CAP", "2")
    get_settings.cache_clear()

    for _ in range(3):
        _walk_to_queue(tenant_id, _pilot_lead(factory_a))

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as pool:
        stats = list(pool.map(lambda _: process_pending(max_jobs=10), range(4)))

    assert sum(s.sent for s in stats) == 2
    assert len(pilot_env.requests) == 2  # the provider saw exactly the cap
    with tenant_session(tenant_id) as session:
        statuses = sorted(session.execute(select(SendJob.status)).scalars().all())
        assert statuses == ["blocked", "sent", "sent"]


def test_one_click_url_carries_a_verifiable_per_job_token(
    tenant_a, factory_a, pilot_env, monkeypatch
):
    """The https unsubscribe target is per-send: the embedded token must
    verify and identify exactly this tenant, lead, and job."""
    from relay.ingest.unsubscribe import verify_token

    tenant_id, _ = tenant_a
    monkeypatch.setenv("RELAY_UNSUBSCRIBE_URL", "https://relay.example/unsubscribe")
    get_settings.cache_clear()
    reset_senders()
    _sender_cache["ses"] = SESSender(client=pilot_env)
    lead_id = _pilot_lead(factory_a)
    _walk_to_queue(tenant_id, lead_id)
    process_pending()

    headers = pilot_env.requests[0]["Content"]["Simple"]["Headers"]
    lu = next(h["Value"] for h in headers if h["Name"] == "List-Unsubscribe")
    token = lu.split("?token=", 1)[1].split(">", 1)[0]
    token_tenant, token_lead, token_job = verify_token(token)
    with tenant_session(tenant_id) as session:
        job = session.execute(select(SendJob)).scalar_one()
        assert (token_tenant, token_lead, token_job) == (
            tenant_id,
            lead_id,
            job.id,
        )


# ── Phase 3 pacing: temporal limits DEFER, they never block ────────────────


def test_hourly_pace_defers_and_later_sends(
    tenant_a, factory_a, pilot_env, monkeypatch
):
    """A paced-out job is deferred — it stays queued and its lead stays in
    send_queued — and goes out on a later tick once the window clears.
    Queue-time eligibility is unaffected by pacing."""
    tenant_id, _ = tenant_a
    monkeypatch.setenv("RELAY_REAL_SEND_HOURLY_CAP", "1")
    get_settings.cache_clear()

    first = _pilot_lead(factory_a)
    _walk_to_queue(tenant_id, first)
    assert process_pending().sent == 1

    # The second lead QUEUES fine (pacing is execution-time only)…
    second = _pilot_lead(factory_a)
    _walk_to_queue(tenant_id, second)
    # …but execution defers it, leaving the job and lead untouched.
    stats = process_pending()
    assert (stats.sent, stats.blocked, stats.deferred) == (0, 0, 1)
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, second)
        job = session.execute(
            select(SendJob).where(SendJob.lead_id == second)
        ).scalar_one()
        assert job.status == "queued" and lead.state == "send_queued"

    # The hour passes (backdate the first send): the deferred job goes out.
    with tenant_session(tenant_id) as session:
        session.execute(
            text(
                "UPDATE send_jobs SET started_at = started_at"
                " - interval '2 hours' WHERE lead_id = :lead"
            ),
            {"lead": str(first)},
        )
    assert process_pending().sent == 1
    assert len(pilot_env.requests) == 2


def test_min_spacing_defers_back_to_back_sends(
    tenant_a, factory_a, pilot_env, monkeypatch
):
    tenant_id, _ = tenant_a
    monkeypatch.setenv("RELAY_REAL_SEND_MIN_SPACING_SECONDS", "600")
    get_settings.cache_clear()

    for lead_id in (_pilot_lead(factory_a), _pilot_lead(factory_a)):
        _walk_to_queue(tenant_id, lead_id)

    stats = process_pending()
    assert stats.sent == 1 and stats.deferred == 1

    with tenant_session(tenant_id) as session:
        session.execute(
            text(
                "UPDATE send_jobs SET started_at = started_at"
                " - interval '11 minutes' WHERE status = 'sent'"
            )
        )
    assert process_pending().sent == 1


def test_warmup_ramp_caps_a_young_identity(tenant_a, factory_a, pilot_env, monkeypatch):
    """Day 0 of warmup allows only warmup_daily_start sends even when the
    configured daily cap is higher; the block reason names the ramp. A day
    later the ramp has risen and sending resumes."""
    tenant_id, _ = tenant_a
    monkeypatch.setenv("RELAY_WARMUP_DAILY_START", "1")
    monkeypatch.setenv("RELAY_WARMUP_DAILY_INCREMENT", "5")
    get_settings.cache_clear()

    first = _pilot_lead(factory_a)
    _walk_to_queue(tenant_id, first)
    assert process_pending().sent == 1  # the day-0 allowance

    second = _pilot_lead(factory_a)
    run_to_approval(tenant_id, second)
    approve_current_draft(tenant_id, second)
    from relay.pipeline.runner import PipelineRunner

    outcome = PipelineRunner(tenant_id, lead_id=second).run()
    assert outcome.final_state == "send_blocked"
    with tenant_session(tenant_id) as session:
        from relay.db.models import LeadTransition

        reason = session.execute(
            select(LeadTransition.reason).where(
                LeadTransition.lead_id == second,
                LeadTransition.to_state == "send_blocked",
            )
        ).scalar_one()
        assert "warmup day 0" in (reason or "")

    # A day later: ramp = 1 + 5·1 = 6, so the configured cap (5) rules
    # again; the first send has also left the 24h window.
    with tenant_session(tenant_id) as session:
        session.execute(
            text(
                "UPDATE send_jobs SET started_at = started_at"
                " - interval '25 hours' WHERE status = 'sent'"
            )
        )
    third = _pilot_lead(factory_a)
    _walk_to_queue(tenant_id, third)
    assert process_pending().sent == 1
