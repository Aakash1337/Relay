"""SES/SNS event ingestion: authenticated, idempotent, PII-clean."""

from __future__ import annotations

import base64
import datetime as dt
import json
import uuid

import pytest
from sqlalchemy import select

from relay.config import get_settings
from relay.db.engine import tenant_session
from relay.db.models import Lead, Suppression
from relay.hashing import hash_email
from relay.ingest.ses_events import (
    EventRejected,
    process_sns_envelope,
    verify_sns_signature,
)
from tests.conftest import walk_to_closed, walk_to_sent

pytestmark = pytest.mark.exit_gate

_TRUST_ALL = lambda envelope: None  # noqa: E731 — injected verifier for unit tests


def _envelope(event: dict, msg_type: str = "Notification") -> bytes:
    return json.dumps(
        {
            "Type": msg_type,
            "MessageId": str(uuid.uuid4()),
            "TopicArn": "arn:aws:sns:eu-central-1:000000000000:relay-ses",
            "Message": json.dumps(event),
            "Timestamp": "2026-07-05T12:00:00.000Z",
            "SignatureVersion": "1",
            "Signature": "ZmFrZQ==",
            "SigningCertURL": "https://sns.eu-central-1.amazonaws.com/cert.pem",
        }
    ).encode()


def _bounce_event(email: str, bounce_type: str = "Permanent") -> dict:
    return {
        "notificationType": "Bounce",
        "bounce": {
            "bounceType": bounce_type,
            "bouncedRecipients": [{"emailAddress": email}],
        },
        "mail": {"destination": [email]},
    }


def _complaint_event(email: str) -> dict:
    return {
        "notificationType": "Complaint",
        "complaint": {"complainedRecipients": [{"emailAddress": email}]},
        "mail": {"destination": [email]},
    }


# ── Event → machinery mapping ───────────────────────────────────────────────


def test_hard_bounce_transitions_lead_and_auto_suppresses(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    email = f"bounce-{uuid.uuid4().hex[:6]}@example.test"
    lead_id = factory_a.lead(email=email)
    walk_to_sent(tenant_id, lead_id)

    stats = process_sns_envelope(_envelope(_bounce_event(email)), verifier=_TRUST_ALL)
    assert stats.bounces == 1

    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None and lead.state == "bounce_received"
        # One signal, one transition, one suppression entry (the trigger).
        entries = (
            session.execute(
                select(Suppression).where(Suppression.email_hash == hash_email(email))
            )
            .scalars()
            .all()
        )
        assert len(entries) == 1 and entries[0].reason == "hard_bounce"

    # Replay: the lead is terminal, nothing moves, nothing duplicates.
    stats = process_sns_envelope(_envelope(_bounce_event(email)), verifier=_TRUST_ALL)
    assert stats.bounces == 0 and stats.ignored >= 1
    with tenant_session(tenant_id) as session:
        entries = (
            session.execute(
                select(Suppression).where(Suppression.email_hash == hash_email(email))
            )
            .scalars()
            .all()
        )
        assert len(entries) == 1


def test_soft_bounce_is_logged_not_suppressed(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    email = f"soft-{uuid.uuid4().hex[:6]}@example.test"
    lead_id = factory_a.lead(email=email)
    walk_to_sent(tenant_id, lead_id)

    stats = process_sns_envelope(
        _envelope(_bounce_event(email, bounce_type="Transient")),
        verifier=_TRUST_ALL,
    )
    assert stats.bounces == 0
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None and lead.state == "sent"  # untouched


def test_complaint_suppresses_once(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    email = f"complain-{uuid.uuid4().hex[:6]}@example.test"
    lead_id = factory_a.lead(email=email)
    walk_to_sent(tenant_id, lead_id)

    for _ in range(2):  # original + provider redelivery
        process_sns_envelope(_envelope(_complaint_event(email)), verifier=_TRUST_ALL)

    with tenant_session(tenant_id) as session:
        entries = (
            session.execute(
                select(Suppression).where(
                    Suppression.email_hash == hash_email(email),
                    Suppression.reason == "complaint",
                )
            )
            .scalars()
            .all()
        )
        assert len(entries) == 1


def test_event_for_unknown_recipient_is_ignored():
    stats = process_sns_envelope(
        _envelope(_bounce_event("stranger@example.test")), verifier=_TRUST_ALL
    )
    assert stats.bounces == 0 and stats.ignored == 1


def test_subscription_confirmation_uses_injected_confirmer():
    confirmed: list[str] = []
    raw = json.dumps(
        {
            "Type": "SubscriptionConfirmation",
            "MessageId": "m",
            "Token": "t",
            "TopicArn": "arn:aws:sns:eu-central-1:0:relay-ses",
            "Message": "confirm me",
            "SubscribeURL": "https://sns.eu-central-1.amazonaws.com/confirm",
            "Timestamp": "2026-07-05T12:00:00.000Z",
        }
    )
    process_sns_envelope(
        raw, verifier=_TRUST_ALL, confirm_subscription=confirmed.append
    )
    assert confirmed == ["https://sns.eu-central-1.amazonaws.com/confirm"]


# ── Signature verification (real crypto, self-signed cert) ─────────────────


def _make_cert_and_signer():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.test")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM)

    def sign(canonical: bytes) -> str:
        return base64.b64encode(
            key.sign(canonical, padding.PKCS1v15(), hashes.SHA1())  # noqa: S303
        ).decode()

    return pem, sign


def _signed_envelope(sign, message: str) -> dict:
    fields = {
        "Type": "Notification",
        "MessageId": "mid-1",
        "Message": message,
        "Timestamp": "2026-07-05T12:00:00.000Z",
        "TopicArn": "arn:aws:sns:eu-central-1:0:relay-ses",
    }
    canonical = "".join(
        f"{k}\n{fields[k]}\n"
        for k in ("Message", "MessageId", "Timestamp", "TopicArn", "Type")
    ).encode()
    return {
        **fields,
        "SignatureVersion": "1",
        "Signature": sign(canonical),
        "SigningCertURL": "https://sns.eu-central-1.amazonaws.com/cert.pem",
    }


def test_valid_signature_passes_and_tampering_fails():
    pem, sign = _make_cert_and_signer()
    envelope = _signed_envelope(sign, "hello")
    verify_sns_signature(envelope, cert_fetcher=lambda url: pem)  # no raise

    tampered = dict(envelope, Message="attacker changed this")
    with pytest.raises(EventRejected, match="signature"):
        verify_sns_signature(tampered, cert_fetcher=lambda url: pem)


def test_cert_must_come_from_amazonaws():
    pem, sign = _make_cert_and_signer()
    envelope = _signed_envelope(sign, "hello")
    envelope["SigningCertURL"] = "https://evil.example.com/cert.pem"
    with pytest.raises(EventRejected, match="untrusted SigningCertURL"):
        verify_sns_signature(envelope, cert_fetcher=lambda url: pem)


# ── HTTP + SQS transports ────────────────────────────────────────────────────


def test_webhook_requires_token(client, monkeypatch):
    monkeypatch.setenv("RELAY_SES_WEBHOOK_TOKEN", "hook-secret")
    get_settings.cache_clear()
    try:
        assert client.post("/webhooks/ses", content=b"{}").status_code == 403
        assert (
            client.post("/webhooks/ses?token=wrong", content=b"{}").status_code == 403
        )
        # Right token, bad envelope: authenticated but rejected → 400.
        response = client.post("/webhooks/ses?token=hook-secret", content=b"{}")
        assert response.status_code == 400
    finally:
        get_settings.cache_clear()


def test_webhook_disabled_without_configured_token(client):
    assert client.post("/webhooks/ses?token=", content=b"{}").status_code == 403


def test_sqs_poller_processes_and_deletes(tenant_a, factory_a, monkeypatch):
    tenant_id, _ = tenant_a
    email = f"sqs-{uuid.uuid4().hex[:6]}@example.test"
    lead_id = factory_a.lead(email=email)
    walk_to_sent(tenant_id, lead_id)

    class FakeSQS:
        def __init__(self, bodies):
            self.bodies = bodies
            self.deleted = []

        def receive_message(self, **kwargs):
            return {
                "Messages": [
                    {"Body": b.decode(), "ReceiptHandle": f"rh-{i}"}
                    for i, b in enumerate(self.bodies)
                ]
            }

        def delete_message(self, QueueUrl, ReceiptHandle):  # noqa: N803
            self.deleted.append(ReceiptHandle)

    monkeypatch.setenv("RELAY_SQS_QUEUE_URL", "https://sqs.fake/queue")
    get_settings.cache_clear()
    import relay.workers.event_worker as event_worker

    monkeypatch.setattr(
        event_worker,
        "process_sns_envelope",
        lambda body: process_sns_envelope(body, verifier=_TRUST_ALL),
    )
    fake = FakeSQS([_envelope(_bounce_event(email)), b"not-json"])
    stats = event_worker.poll_once(client=fake)
    get_settings.cache_clear()

    assert stats.received == 2
    assert stats.processed == 1
    assert stats.rejected == 1  # malformed message logged + dropped
    assert len(fake.deleted) == 2  # both removed from the queue
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None and lead.state == "bounce_received"


# ── Cert-host pinning (the SNS auth-bypass fix) ────────────────────────────


def test_cert_host_must_be_the_sns_service_host():
    """A bare .endswith('.amazonaws.com') accepted attacker-controlled AWS
    endpoints (S3, API Gateway) where anyone can host a self-signed cert.
    Only sns.<region>.amazonaws.com is trusted now."""
    pem, sign = _make_cert_and_signer()
    for bad_host in (
        "https://attacker-bucket.s3.us-east-1.amazonaws.com/cert.pem",
        "https://foo.execute-api.us-east-1.amazonaws.com/cert.pem",
        "https://sns.eu-central-1.amazonaws.com.evil.com/cert.pem",
        "http://sns.eu-central-1.amazonaws.com/cert.pem",  # not https
    ):
        env = _signed_envelope(sign, "hello")
        env["SigningCertURL"] = bad_host
        with pytest.raises(EventRejected, match="untrusted SigningCertURL"):
            verify_sns_signature(env, cert_fetcher=lambda url: pem)


def test_default_confirm_rejects_non_sns_host():
    from relay.ingest.ses_events import _default_confirm

    with pytest.raises(EventRejected, match="untrusted SubscribeURL"):
        _default_confirm("https://attacker.s3.amazonaws.com/confirm")


# ── Bounce suppression is decoupled from the 'sent' transition ─────────────


def test_hard_bounce_suppresses_even_when_lead_not_in_sent(tenant_a, factory_a):
    """A hard bounce for a lead that already moved past 'sent' (or a replay)
    must STILL suppress — a dead address is a do-not-contact signal
    regardless of pipeline state, unlike the transition which needs 'sent'."""
    tenant_id, _ = tenant_a
    email = f"late-bounce-{uuid.uuid4().hex[:6]}@example.test"
    lead_id = factory_a.lead(email=email)
    walk_to_closed(tenant_id, lead_id)  # lead is now 'closed', not 'sent'

    stats = process_sns_envelope(_envelope(_bounce_event(email)), verifier=_TRUST_ALL)
    assert stats.bounces == 1
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None and lead.state == "closed"  # not transitioned
        entry = session.execute(
            select(Suppression).where(
                Suppression.email_hash == hash_email(email),
                Suppression.reason == "hard_bounce",
            )
        ).scalar_one()  # but suppressed anyway
        assert entry.source == "provider_webhook"

    # Idempotent: a replay writes no second entry.
    process_sns_envelope(_envelope(_bounce_event(email)), verifier=_TRUST_ALL)
    with tenant_session(tenant_id) as session:
        entries = (
            session.execute(
                select(Suppression).where(
                    Suppression.email_hash == hash_email(email),
                    Suppression.reason == "hard_bounce",
                )
            )
            .scalars()
            .all()
        )
        assert len(entries) == 1


# ── One bounce signal, every affected lead ──────────────────────────────────


def test_hard_bounce_moves_every_sent_lead_for_the_address(tenant_a, factory_a):
    """One dead address can sit in several campaigns: a single hard bounce
    must move EVERY lead in 'sent' for that hash — while the auto-suppress
    trigger still writes exactly one suppression entry (the reputation
    threshold counts entries, so one signal must weigh once)."""
    tenant_id, _ = tenant_a
    email = f"multi-{uuid.uuid4().hex[:6]}@example.test"
    first = factory_a.lead(email=email)
    second = factory_a.lead(email=email)  # its own campaign (factory default)
    walk_to_sent(tenant_id, first)
    walk_to_sent(tenant_id, second)

    stats = process_sns_envelope(_envelope(_bounce_event(email)), verifier=_TRUST_ALL)
    assert stats.bounces == 1

    with tenant_session(tenant_id) as session:
        for lead_id in (first, second):
            lead = session.get(Lead, lead_id)
            assert lead is not None and lead.state == "bounce_received"
        entries = (
            session.execute(
                select(Suppression).where(
                    Suppression.email_hash == hash_email(email),
                    Suppression.reason == "hard_bounce",
                )
            )
            .scalars()
            .all()
        )
        assert len(entries) == 1


def test_malformed_recipient_is_ignored_not_poisonous():
    """A recipient with no domain part cannot be ours; it must be counted
    as ignored — never raised — or one bad event would poison the whole
    envelope (and, via the SQS path, stall the queue)."""
    stats = process_sns_envelope(
        _envelope(_bounce_event("not-an-email")), verifier=_TRUST_ALL
    )
    assert stats.bounces == 0 and stats.ignored == 1


def test_sqs_poller_isolates_unexpected_failures(tenant_a, factory_a, monkeypatch):
    """An UNEXPECTED per-message failure (DB outage mid-write, unlike a
    rejected envelope) must not stall the rest of the batch — and the
    failed message must stay in the queue for redelivery."""
    tenant_id, _ = tenant_a
    email = f"iso-{uuid.uuid4().hex[:6]}@example.test"
    lead_id = factory_a.lead(email=email)
    walk_to_sent(tenant_id, lead_id)

    class FakeSQS:
        def __init__(self, bodies):
            self.bodies = bodies
            self.deleted = []

        def receive_message(self, **kwargs):
            return {
                "Messages": [
                    {"Body": b.decode(), "ReceiptHandle": f"rh-{i}"}
                    for i, b in enumerate(self.bodies)
                ]
            }

        def delete_message(self, QueueUrl, ReceiptHandle):  # noqa: N803
            self.deleted.append(ReceiptHandle)

    monkeypatch.setenv("RELAY_SQS_QUEUE_URL", "https://sqs.fake/queue")
    get_settings.cache_clear()
    import relay.workers.event_worker as event_worker

    def handler(body):
        if body == "boom":
            raise RuntimeError("simulated db outage")
        return process_sns_envelope(body, verifier=_TRUST_ALL)

    monkeypatch.setattr(event_worker, "process_sns_envelope", handler)
    fake = FakeSQS([b"boom", _envelope(_bounce_event(email))])
    stats = event_worker.poll_once(client=fake)
    get_settings.cache_clear()

    assert stats.received == 2
    assert stats.errored == 1
    assert stats.processed == 1
    assert fake.deleted == ["rh-1"]  # poison message left for redelivery
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None and lead.state == "bounce_received"
