"""One-click unsubscribe (RFC 8058): signed tokens, idempotent, decoupled.

The compliance invariant under test: once a recipient unsubscribes —
via the mail provider's one-click POST or the landing-page button — a
suppression entry exists and no future send to that address is
eligible, regardless of what state their lead was in.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from relay.db.engine import tenant_session
from relay.db.models import Lead, SendJob, Suppression
from relay.hashing import hash_email
from relay.ingest.unsubscribe import (
    UnsubscribeRejected,
    build_token,
    process_unsubscribe,
    verify_token,
)
from tests.conftest import (
    approve_current_draft,
    run_to_approval,
    walk_to_closed,
    walk_to_sent,
)

pytestmark = pytest.mark.exit_gate


def _sent_lead_token(tenant_id, factory) -> tuple[uuid.UUID, str]:
    """A lead walked to 'sent' plus the token its send would embed."""
    email = f"unsub-{uuid.uuid4().hex[:6]}@example.test"
    lead_id = factory.lead(email=email)
    walk_to_sent(tenant_id, lead_id)
    with tenant_session(tenant_id) as session:
        job_id = session.execute(
            select(SendJob.id).where(SendJob.lead_id == lead_id)
        ).scalar_one()
    return lead_id, build_token(tenant_id, lead_id, job_id)


# ── The token itself ────────────────────────────────────────────────────────


def test_token_round_trips_and_rejects_tampering(tenant_a):
    tenant_id, _ = tenant_a
    lead_id, job_id = uuid.uuid4(), uuid.uuid4()
    token = build_token(tenant_id, lead_id, job_id)
    assert verify_token(token) == (tenant_id, lead_id, job_id)

    # Flip the last signature character.
    bad_sig = token[:-1] + ("0" if token[-1] != "0" else "1")
    with pytest.raises(UnsubscribeRejected, match="signature"):
        verify_token(bad_sig)

    # Swap in another tenant id, keeping the signature: the key is
    # derived from the tenant id inside the token, so this cannot verify.
    parts = token.split(".")
    parts[1] = uuid.uuid4().hex
    with pytest.raises(UnsubscribeRejected, match="signature"):
        verify_token(".".join(parts))

    for garbage in ("", "v1.zzz", "v0." + ".".join(parts[1:]), "a.b.c.d.e"):
        with pytest.raises(UnsubscribeRejected):
            verify_token(garbage)


# ── Processing: transition where legal, suppress always ────────────────────


def test_one_click_unsubscribes_a_sent_lead_idempotently(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id, token = _sent_lead_token(tenant_id, factory_a)

    assert process_unsubscribe(token) is True
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None and lead.state == "unsubscribed"
        entries = (
            session.execute(
                select(Suppression).where(
                    Suppression.email_hash == lead.email_hash,
                    Suppression.reason == "unsubscribe",
                )
            )
            .scalars()
            .all()
        )
        assert len(entries) == 1  # written by fn_auto_suppress, once

    # Provider retry / double click: nothing moves, nothing duplicates.
    assert process_unsubscribe(token) is False
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        entries = (
            session.execute(
                select(Suppression).where(
                    Suppression.email_hash == lead.email_hash,
                    Suppression.reason == "unsubscribe",
                )
            )
            .scalars()
            .all()
        )
        assert lead.state == "unsubscribed" and len(entries) == 1


def test_unsubscribe_for_terminal_lead_still_suppresses(tenant_a, factory_a):
    """A lead already terminal (closed) keeps its state honest — but the
    do-not-contact signal MUST still land, decoupled from the machine."""
    tenant_id, _ = tenant_a
    email = f"closed-{uuid.uuid4().hex[:6]}@example.test"
    lead_id = factory_a.lead(email=email)
    walk_to_closed(tenant_id, lead_id)
    with tenant_session(tenant_id) as session:
        job_id = session.execute(
            select(SendJob.id).where(SendJob.lead_id == lead_id)
        ).scalar_one()

    assert process_unsubscribe(build_token(tenant_id, lead_id, job_id)) is True
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None and lead.state == "closed"  # untouched
        entry = session.execute(
            select(Suppression).where(
                Suppression.email_hash == hash_email(email),
                Suppression.reason == "unsubscribe",
            )
        ).scalar_one()
        assert entry.source == "link"


def test_unsubscribed_recipient_can_never_be_resent(tenant_a, factory_a):
    """The §10 invariant end to end: after a one-click unsubscribe, the
    same address in a brand-new campaign is blocked at eligibility."""
    tenant_id, _ = tenant_a
    lead_id, token = _sent_lead_token(tenant_id, factory_a)
    process_unsubscribe(token)
    with tenant_session(tenant_id) as session:
        email = session.get(Lead, lead_id).email

    second = factory_a.lead(email=email)  # fresh campaign, same address
    run_to_approval(tenant_id, second)
    approve_current_draft(tenant_id, second)
    from relay.pipeline.runner import PipelineRunner

    outcome = PipelineRunner(tenant_id, lead_id=second).run()
    assert outcome.final_state == "send_blocked"


# ── The HTTP boundary ───────────────────────────────────────────────────────


def test_get_renders_confirmation_and_never_mutates(client, tenant_a, factory_a):
    """Mail clients and security scanners prefetch GET links: the landing
    page must not unsubscribe anyone. Only the POST acts."""
    tenant_id, _ = tenant_a
    lead_id, token = _sent_lead_token(tenant_id, factory_a)

    response = client.get("/unsubscribe", params={"token": token})
    assert response.status_code == 200
    assert "<form" in response.text  # the human confirms via POST
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None and lead.state == "sent"  # untouched

    response = client.post("/unsubscribe", params={"token": token})
    assert response.status_code == 200
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None and lead.state == "unsubscribed"


def test_http_rejects_bad_tokens(client):
    assert client.get("/unsubscribe", params={"token": "garbage"}).status_code == 400
    assert client.post("/unsubscribe", params={"token": "garbage"}).status_code == 400
    assert client.post("/unsubscribe").status_code == 400


def test_unsubscribe_for_erased_lead_is_a_quiet_noop(tenant_a, factory_a):
    """After DSR erasure the lead row is gone (a hashed do-not-contact
    entry already exists): a late unsubscribe click must not error and
    must not reveal anything."""
    tenant_id, _ = tenant_a
    lead_id, token = _sent_lead_token(tenant_id, factory_a)
    with tenant_session(tenant_id) as session:
        email = session.get(Lead, lead_id).email
    from relay.domain.dsr import execute_erasure

    execute_erasure(tenant_id, email=email, requested_by="test-dsr")
    assert process_unsubscribe(token) is False
