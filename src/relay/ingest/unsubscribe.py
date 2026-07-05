"""One-click unsubscribe (RFC 8058) — signed tokens, idempotent processing.

Every real send embeds a per-job HTTPS unsubscribe URL (next to the
mailto) in its List-Unsubscribe header when ``RELAY_UNSUBSCRIBE_URL`` is
configured. The token identifies (tenant, lead, send job) and is signed
with a per-tenant key derived from the master key — it carries no PII,
cannot be forged without the master key, and a token minted for one
tenant cannot act on another (the signature is checked with the key
derived from the tenant id INSIDE the token, so tampering with the
tenant id breaks the signature).

Processing mirrors the bounce handler's decoupling: the suppression
entry is the compliance guarantee and ALWAYS lands (idempotently); the
lead transition to ``unsubscribed`` is taken only where the state
machine allows it (``sent`` — the no-reply one-click moment; a lead
mid-triage or already terminal keeps its state, suppressed anyway).

Mail providers may retry the POST and link scanners may prefetch the
GET: processing is idempotent, and the GET route never mutates state.
"""

from __future__ import annotations

import hmac
import uuid

from sqlalchemy import select

from relay import audit
from relay.config import get_settings
from relay.db.engine import tenant_session
from relay.db.models import Lead, Suppression
from relay.domain.state_machine import transition
from relay.domain.states import LeadState, is_transition_allowed
from relay.domain.suppression import add_suppression
from relay.hashing import derive_tenant_key
from relay.logs import get_logger

log = get_logger(__name__)

ACTOR = "webhook:unsubscribe"

_VERSION = "v1"


class UnsubscribeRejected(Exception):
    """The token failed authentication or was structurally invalid."""


def _sign(
    master_key: str, tenant_id: uuid.UUID, lead_id: uuid.UUID, job_id: uuid.UUID
) -> str:
    key = derive_tenant_key(master_key, str(tenant_id), "unsubscribe")
    payload = f"{_VERSION}.{tenant_id.hex}.{lead_id.hex}.{job_id.hex}"
    return hmac.new(key, payload.encode("utf-8"), "sha256").hexdigest()


def build_token(tenant_id: uuid.UUID, lead_id: uuid.UUID, job_id: uuid.UUID) -> str:
    """Mint the signed token embedded in a send's List-Unsubscribe URL.

    Always signs with the CURRENT master key — the previous key (if any)
    is verify-only during a rotation window.
    """
    key = get_settings().master_key.get_secret_value()
    sig = _sign(key, tenant_id, lead_id, job_id)
    return f"{_VERSION}.{tenant_id.hex}.{lead_id.hex}.{job_id.hex}.{sig}"


def verify_token(token: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Return (tenant_id, lead_id, job_id) for a valid token, else raise.

    Verification accepts the current master key and — during a rotation
    window — RELAY_MASTER_KEY_PREVIOUS, so unsubscribe links already
    sitting in delivered mail keep working across a rotation. An
    unsubscribe link that silently dies IS a compliance failure.
    """
    parts = token.split(".")
    if len(parts) != 5 or parts[0] != _VERSION:
        raise UnsubscribeRejected("malformed unsubscribe token")
    try:
        tenant_id = uuid.UUID(hex=parts[1])
        lead_id = uuid.UUID(hex=parts[2])
        job_id = uuid.UUID(hex=parts[3])
    except ValueError as exc:
        raise UnsubscribeRejected("malformed unsubscribe token") from exc
    settings = get_settings()
    keys = [settings.master_key.get_secret_value()]
    if settings.master_key_previous is not None:
        keys.append(settings.master_key_previous.get_secret_value())
    for master_key in keys:
        if hmac.compare_digest(parts[4], _sign(master_key, tenant_id, lead_id, job_id)):
            return tenant_id, lead_id, job_id
    raise UnsubscribeRejected("bad unsubscribe token signature")


def process_unsubscribe(token: str) -> bool:
    """Honor an unsubscribe. Returns True if it was newly recorded.

    Idempotent: a replayed token (provider retry, double click) finds the
    suppression entry already present and reports success without writing
    a second one.
    """
    tenant_id, lead_id, job_id = verify_token(token)
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            # The lead was erased (DSR) or purged (retention). Erasure
            # already left a do-not-contact suppression entry, so there
            # is nothing to record — and nothing to reveal to the caller.
            log.info("unsubscribe for absent lead", send_job_id=str(job_id))
            return False
        if is_transition_allowed(LeadState(lead.state), LeadState.UNSUBSCRIBED):
            # fn_auto_suppress writes the 'unsubscribe' suppression entry
            # in this same transaction, idempotently.
            transition(
                session,
                lead,
                LeadState.UNSUBSCRIBED,
                actor=ACTOR,
                reason="one-click unsubscribe (RFC 8058)",
            )
            newly_recorded = True
        else:
            # The lead is mid-triage or terminal: keep its state honest,
            # but the do-not-contact signal MUST still land.
            existing = (
                session.execute(
                    select(Suppression).where(
                        Suppression.tenant_id == tenant_id,
                        Suppression.email_hash == lead.email_hash,
                        Suppression.reason == "unsubscribe",
                    )
                )
                .scalars()
                .first()
            )
            if existing is None:
                add_suppression(
                    session,
                    tenant_id=tenant_id,
                    reason="unsubscribe",
                    source="link",
                    created_by=ACTOR,
                    email=lead.email,
                    scope="tenant",
                )
                newly_recorded = True
            else:
                newly_recorded = False
        audit.record(
            session,
            tenant_id=tenant_id,
            actor_type="system",
            actor_id=ACTOR,
            action="unsubscribe.received",
            entity_type="send_job",
            entity_id=str(job_id),
            payload={
                "lead_id": str(lead_id),
                "email_hash": lead.email_hash,
                "newly_recorded": newly_recorded,
            },
        )
    log.info(
        "unsubscribe processed",
        lead_id=str(lead_id),
        send_job_id=str(job_id),
        newly_recorded=newly_recorded,
    )
    return newly_recorded
