"""SES event ingestion via SNS envelopes — signature-verified, idempotent.

Transport-agnostic: the HTTPS webhook route and the SQS poller both feed
raw SNS envelopes into :func:`process_sns_envelope`. Every envelope is
signature-verified against the AWS signing certificate before anything
is parsed further (the verifier and its cert fetcher are injectable for
hermetic tests; production uses the real one).

Event mapping (idempotent under provider redelivery, which SNS/SES
explicitly do):

- **Bounce (Permanent)** → the lead (found by recipient hash, in state
  ``sent``) transitions to ``bounce_received``; the existing
  ``fn_auto_suppress`` trigger writes the hard-bounce suppression entry
  in the same transaction. A replayed bounce finds the lead already
  terminal and does nothing.
- **Bounce (Transient)** → logged only; a soft bounce is not a
  do-not-contact signal.
- **Complaint** → suppression entry (reason ``complaint``, source
  ``provider_webhook``) unless one already exists for the hash.
- **Delivery** → audit record only.

PII: the raw recipient address from the event is hashed immediately and
never logged or stored.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx
from sqlalchemy import select, text

from relay import audit
from relay.db.engine import tenant_session, untenanted_app_session
from relay.db.models import Lead, Suppression
from relay.domain.state_machine import transition
from relay.domain.states import LeadState
from relay.domain.suppression import add_suppression
from relay.hashing import email_domain, hash_email
from relay.logs import get_logger

log = get_logger(__name__)

ACTOR = "worker:ses-events"


class EventRejected(Exception):
    """The envelope failed authentication or was structurally invalid."""


@dataclass
class IngestStats:
    bounces: int = 0
    complaints: int = 0
    deliveries: int = 0
    ignored: int = 0
    tenants: set = field(default_factory=set)


# ── SNS signature verification ──────────────────────────────────────────────

_SIGNED_FIELDS = {
    "Notification": (
        "Message",
        "MessageId",
        "Subject",
        "Timestamp",
        "TopicArn",
        "Type",
    ),
    "SubscriptionConfirmation": (
        "Message",
        "MessageId",
        "SubscribeURL",
        "Timestamp",
        "Token",
        "TopicArn",
        "Type",
    ),
    "UnsubscribeConfirmation": (
        "Message",
        "MessageId",
        "SubscribeURL",
        "Timestamp",
        "Token",
        "TopicArn",
        "Type",
    ),
}


def _default_cert_fetcher(url: str) -> bytes:
    return httpx.get(url, timeout=10.0).content


#: SNS signing certs and confirmation URLs live ONLY on the SNS service
#: host: sns.<region>.amazonaws.com. A bare ``.endswith(".amazonaws.com")``
#: also accepts attacker-controlled AWS endpoints (an S3 bucket, an API
#: Gateway stage, …) where anyone can host a self-signed cert — which,
#: with no chain validation, fully bypasses envelope authentication. Pin
#: the exact service host instead.
_SNS_HOST_RE = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com$")


def _require_sns_host(url: str, *, what: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not _SNS_HOST_RE.fullmatch(parsed.hostname or ""):
        raise EventRejected(f"untrusted {what} host: {url[:100]}")


def verify_sns_signature(
    envelope: dict,
    *,
    cert_fetcher: Callable[[str], bytes] = _default_cert_fetcher,
) -> None:
    """Verify the envelope per the SNS spec. Raises EventRejected.

    The signing certificate must come from an https URL on an
    amazonaws.com host — a forged envelope cannot point at its own cert.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.x509 import load_pem_x509_certificate

    cert_url = envelope.get("SigningCertURL", "")
    _require_sns_host(cert_url, what="SigningCertURL")

    msg_type = envelope.get("Type", "")
    fields = _SIGNED_FIELDS.get(msg_type)
    if fields is None:
        raise EventRejected(f"unknown SNS message type: {msg_type!r}")
    canonical = "".join(
        f"{name}\n{envelope[name]}\n" for name in fields if name in envelope
    ).encode()

    try:
        cert = load_pem_x509_certificate(cert_fetcher(cert_url))
        signature = base64.b64decode(envelope.get("Signature", ""))
        algorithm = (
            hashes.SHA256()
            if envelope.get("SignatureVersion") == "2"
            else hashes.SHA1()  # noqa: S303 — SNS SignatureVersion 1 is SHA1 by spec
        )
        cert.public_key().verify(  # type: ignore[union-attr]
            signature, canonical, padding.PKCS1v15(), algorithm
        )
    except InvalidSignature as exc:
        raise EventRejected("SNS signature verification failed") from exc
    except EventRejected:
        raise
    except Exception as exc:  # certificate parse/fetch failures
        raise EventRejected(f"SNS signature check errored: {exc}") from exc


# ── Envelope processing ─────────────────────────────────────────────────────


def process_sns_envelope(
    raw: bytes | str,
    *,
    verifier: Callable[..., None] = verify_sns_signature,
    confirm_subscription: Callable[[str], None] | None = None,
) -> IngestStats:
    """Authenticate and process one SNS envelope. Returns stats."""
    stats = IngestStats()
    try:
        envelope = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise EventRejected(f"envelope is not JSON: {exc}") from exc
    if not isinstance(envelope, dict):
        raise EventRejected("envelope is not a JSON object")

    verifier(envelope)

    msg_type = envelope.get("Type")
    if msg_type == "SubscriptionConfirmation":
        url = envelope.get("SubscribeURL", "")
        if confirm_subscription is None:
            confirm_subscription = _default_confirm
        confirm_subscription(url)
        log.info("sns subscription confirmed")
        return stats
    if msg_type != "Notification":
        stats.ignored += 1
        return stats

    try:
        event = json.loads(envelope.get("Message", ""))
    except (TypeError, ValueError) as exc:
        raise EventRejected(f"SES event payload is not JSON: {exc}") from exc

    _process_ses_event(event, stats)
    return stats


def _default_confirm(url: str) -> None:
    # Same pinning as the cert URL: confirming a subscription is a GET to
    # an attacker-influenced URL, so it must be the real SNS host or the
    # confirmation becomes a constrained SSRF.
    _require_sns_host(url, what="SubscribeURL")
    httpx.get(url, timeout=10.0)


def _event_kind(event: dict) -> str:
    # SES uses eventType (event publishing) or notificationType (legacy).
    return str(event.get("eventType") or event.get("notificationType") or "")


def _recipients(event: dict) -> list[str]:
    kind = _event_kind(event)
    if kind == "Bounce":
        return [
            r.get("emailAddress", "")
            for r in (event.get("bounce") or {}).get("bouncedRecipients", [])
        ]
    if kind == "Complaint":
        return [
            r.get("emailAddress", "")
            for r in (event.get("complaint") or {}).get("complainedRecipients", [])
        ]
    return (event.get("mail") or {}).get("destination", []) or []


def _tenants_for_hash(recipient_hash: str) -> list[uuid.UUID]:
    with untenanted_app_session() as session:
        return list(
            session.execute(
                text("SELECT fn_tenants_for_recipient_hash(:h)"),
                {"h": recipient_hash},
            ).scalars()
        )


def _process_ses_event(event: dict, stats: IngestStats) -> None:
    kind = _event_kind(event)
    for address in _recipients(event):
        if not address:
            continue
        recipient_hash = hash_email(address)
        domain = email_domain(address)
        tenants = _tenants_for_hash(recipient_hash)
        if not tenants:
            log.warning(
                "provider event for unknown recipient",
                kind=kind,
                recipient_hash=recipient_hash,
            )
            stats.ignored += 1
            continue
        for tenant_id in tenants:
            stats.tenants.add(str(tenant_id))
            if kind == "Bounce":
                _handle_bounce(tenant_id, event, address, recipient_hash, stats)
            elif kind == "Complaint":
                _handle_complaint(tenant_id, address, recipient_hash, domain, stats)
            elif kind == "Delivery":
                _handle_delivery(tenant_id, recipient_hash, stats)
            else:
                stats.ignored += 1


def _handle_bounce(
    tenant_id: uuid.UUID,
    event: dict,
    address: str,
    recipient_hash: str,
    stats: IngestStats,
) -> None:
    bounce_type = (event.get("bounce") or {}).get("bounceType", "")
    if bounce_type != "Permanent":
        log.info(
            "soft bounce ignored",
            recipient_hash=recipient_hash,
            bounce_type=bounce_type,
        )
        stats.ignored += 1
        return
    with tenant_session(tenant_id) as session:
        lead = (
            session.execute(
                select(Lead).where(
                    Lead.email_hash == recipient_hash,
                    Lead.state == str(LeadState.SENT),
                )
            )
            .scalars()
            .first()
        )
        if lead is not None:
            # fn_auto_suppress writes the hard-bounce suppression entry in
            # this same transaction as the transition — one signal, one
            # transition, one entry.
            transition(
                session,
                lead,
                LeadState.BOUNCE_RECEIVED,
                actor=ACTOR,
                reason="provider hard bounce (SES)",
            )
        else:
            # No lead is in 'sent' to transition (it already moved on, a
            # replay, or the address is only known from an earlier send).
            # A hard bounce is still a definitive dead-address signal that
            # MUST suppress — decoupled from the transition so it can never
            # be dropped. Idempotent: skip if already hard-bounce-suppressed.
            existing = (
                session.execute(
                    select(Suppression).where(
                        Suppression.email_hash == recipient_hash,
                        Suppression.reason == "hard_bounce",
                    )
                )
                .scalars()
                .first()
            )
            if existing is not None:
                stats.ignored += 1
                return
            add_suppression(
                session,
                tenant_id=tenant_id,
                reason="hard_bounce",
                source="provider_webhook",
                created_by=ACTOR,
                email=address,
                scope="tenant",
            )
    stats.bounces += 1


def _handle_complaint(
    tenant_id: uuid.UUID,
    address: str,
    recipient_hash: str,
    domain: str,
    stats: IngestStats,
) -> None:
    with tenant_session(tenant_id) as session:
        existing = (
            session.execute(
                select(Suppression).where(
                    Suppression.email_hash == recipient_hash,
                    Suppression.reason == "complaint",
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            stats.ignored += 1  # replayed complaint: already suppressed
            return
        add_suppression(
            session,
            tenant_id=tenant_id,
            reason="complaint",
            source="provider_webhook",
            created_by=ACTOR,
            email=address,
            scope="tenant",
            domain=domain,
        )
    stats.complaints += 1


def _handle_delivery(
    tenant_id: uuid.UUID, recipient_hash: str, stats: IngestStats
) -> None:
    with tenant_session(tenant_id) as session:
        audit.record(
            session,
            tenant_id=tenant_id,
            actor_type="worker",
            actor_id=ACTOR,
            action="send.delivered",
            entity_type="send_job",
            entity_id=None,
            payload={"recipient_hash": recipient_hash},
        )
    stats.deliveries += 1
