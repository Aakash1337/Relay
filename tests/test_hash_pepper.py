"""The two implemented §17 decisions: keyed email digests with a
dual-lookup transition, and admin-only global suppression scope."""

from __future__ import annotations

import uuid

import pytest

from relay.config import get_settings
from relay.db.engine import tenant_session
from relay.db.models import Lead, Suppression
from relay.domain.dsr import execute_erasure
from relay.domain.suppression import is_suppressed
from relay.hashing import (
    canonical_email,
    email_hash_candidates,
    hash_email,
    legacy_hash_email,
    sha256_hex,
)
from tests.conftest import (
    ADMIN,
    approve_current_draft,
    run_to_approval,
    walk_to_sent,
)

pytestmark = pytest.mark.exit_gate


# ── The keyed digest itself ─────────────────────────────────────────────────


def test_email_digest_is_keyed_not_plain_sha256():
    email = "person@example.test"
    assert hash_email(email) != legacy_hash_email(email)
    assert legacy_hash_email(email) == sha256_hex(canonical_email(email))
    # Canonicalization still applies before keying.
    assert hash_email("  Person@Example.TEST ") == hash_email(email)


def test_candidates_cover_both_schemes_until_cutover(monkeypatch):
    email = "person@example.test"
    assert email_hash_candidates(email) == (
        hash_email(email),
        legacy_hash_email(email),
    )
    monkeypatch.setenv("RELAY_EMAIL_HASH_LEGACY_LOOKUP", "false")
    get_settings.cache_clear()
    assert email_hash_candidates(email) == (hash_email(email),)
    monkeypatch.delenv("RELAY_EMAIL_HASH_LEGACY_LOOKUP")
    get_settings.cache_clear()


# ── Dual-lookup on the paths that matter ────────────────────────────────────


def test_pre_pepper_suppression_row_still_blocks_the_send(tenant_a, factory_a):
    """A do-not-contact entry written under the OLD unkeyed scheme keeps
    blocking sends through the transition window."""
    tenant_id, _ = tenant_a
    email = f"legacy-{uuid.uuid4().hex[:6]}@example.test"
    with tenant_session(tenant_id) as session:
        session.add(
            Suppression(
                tenant_id=tenant_id,
                scope="tenant",
                email_hash=legacy_hash_email(email),  # pre-pepper digest
                domain="example.test",
                reason="do_not_contact",
                source="manual",
                created_by="test",
            )
        )

    lead_id = factory_a.lead(email=email)
    run_to_approval(tenant_id, lead_id)
    approve_current_draft(tenant_id, lead_id)
    from relay.pipeline.runner import PipelineRunner

    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.final_state == "send_blocked"


def test_cutover_stops_matching_legacy_rows(tenant_a, monkeypatch):
    """RELAY_EMAIL_HASH_LEGACY_LOOKUP=false is the cutover: only the
    peppered digest matches afterwards."""
    tenant_id, _ = tenant_a
    email = f"cutover-{uuid.uuid4().hex[:6]}@example.test"
    with tenant_session(tenant_id) as session:
        session.add(
            Suppression(
                tenant_id=tenant_id,
                scope="tenant",
                email_hash=legacy_hash_email(email),
                domain="example.test",
                reason="do_not_contact",
                source="manual",
                created_by="test",
            )
        )

    def blocked() -> bool:
        with tenant_session(tenant_id) as session:
            return any(
                is_suppressed(session, tenant_id=tenant_id, email_hash=c)
                for c in email_hash_candidates(email)
            )

    assert blocked()
    monkeypatch.setenv("RELAY_EMAIL_HASH_LEGACY_LOOKUP", "false")
    get_settings.cache_clear()
    assert not blocked()
    monkeypatch.delenv("RELAY_EMAIL_HASH_LEGACY_LOOKUP")
    get_settings.cache_clear()


def _pre_pepper_lead(factory, monkeypatch, email: str) -> uuid.UUID:
    """Create a lead exactly as a pre-pepper deployment would have: the
    row (and everything derived from it) carries the legacy digest. The
    DB freezes lead email identity, so simulating history means creating
    it that way, not editing it afterwards."""
    import tests.conftest as conftest_module

    with monkeypatch.context() as patch:
        patch.setattr(conftest_module, "hash_email", legacy_hash_email)
        return factory.lead(email=email)


def test_bounce_still_finds_a_pre_pepper_lead(tenant_a, factory_a, monkeypatch):
    """A lead row stored under the old digest (pre-pepper deployment) must
    still transition when its address hard-bounces — and the tenant lookup
    must still route the event via the legacy job digest."""
    from tests.test_ses_ingest import _TRUST_ALL, _bounce_event, _envelope

    tenant_id, _ = tenant_a
    email = f"oldlead-{uuid.uuid4().hex[:6]}@example.test"
    lead_id = _pre_pepper_lead(factory_a, monkeypatch, email)
    walk_to_sent(tenant_id, lead_id)  # job freezes the lead's legacy digest
    with tenant_session(tenant_id) as session:
        assert session.get(Lead, lead_id).email_hash == legacy_hash_email(email)

    from relay.ingest.ses_events import process_sns_envelope

    stats = process_sns_envelope(_envelope(_bounce_event(email)), verifier=_TRUST_ALL)
    assert stats.bounces == 1
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None and lead.state == "bounce_received"


def test_dsr_erasure_removes_pre_pepper_rows(tenant_a, factory_a, monkeypatch):
    tenant_id, _ = tenant_a
    email = f"erase-{uuid.uuid4().hex[:6]}@example.test"
    lead_id = _pre_pepper_lead(factory_a, monkeypatch, email)

    result = execute_erasure(tenant_id, email=email, requested_by="test-dsr")
    assert str(lead_id) in result.lead_ids
    with tenant_session(tenant_id) as session:
        assert session.get(Lead, lead_id) is None
        # The do-not-contact remnant is written under the NEW keyed digest.
        entry = (
            session.execute(
                Suppression.__table__.select().where(
                    Suppression.email_hash == hash_email(email),
                    Suppression.reason == "legal_delete",
                )
            )
            .mappings()
            .first()
        )
        assert entry is not None


# ── Global scope: admin-only insert (§17, decided) ──────────────────────────


def test_global_suppression_via_admin_endpoint(client, api_tenant, tenant_b):
    email = f"global-{uuid.uuid4().hex[:6]}@example.test"
    payload = {"tenant_id": api_tenant["id"], "email": email}

    # Admin-only: no token 422, wrong token 403.
    assert client.post("/internal/suppression/global", json=payload).status_code == 422
    assert (
        client.post(
            "/internal/suppression/global",
            json=payload,
            headers={"X-Admin-Token": "wrong"},
        ).status_code
        == 403
    )

    response = client.post("/internal/suppression/global", json=payload, headers=ADMIN)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["scope"] == "global"
    assert body["email_hash"] == hash_email(email)

    # It reaches every tenant (the point of global scope).
    with tenant_session(tenant_b[0]) as session:
        assert is_suppressed(
            session, tenant_id=tenant_b[0], email_hash=hash_email(email)
        )

    # Unknown tenant → 404.
    assert (
        client.post(
            "/internal/suppression/global",
            json={"tenant_id": str(uuid.uuid4()), "email": email},
            headers=ADMIN,
        ).status_code
        == 404
    )
