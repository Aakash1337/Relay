"""Per-tenant SES identity automation: provision + poll + auto-attest.

The provider is faked at the same seam production uses (client_factory);
nothing here touches the network. What's under test is the wiring: the
provider gate, idempotent provisioning, the DKIM records handed back,
and the attest flipping exactly once — audited — when SES confirms.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from relay.config import get_settings
from relay.db.engine import admin_session
from relay.db.models import AuditLog, Tenant
from relay.senders import identity as identity_module
from relay.senders.identity import IdentityState, identity_for

ADMIN = {"X-Admin-Token": "test-admin-token"}


class FakeIdentityClient:
    """Provider stub with scriptable verification state."""

    def __init__(self, *, verified: bool = False, status: str = "PENDING"):
        self.verified = verified
        self.status_value = status
        self.provisioned: list[str] = []
        self.polled: list[str] = []

    def provision(self, identity: str) -> IdentityState:
        self.provisioned.append(identity)
        return self.status(identity)

    def status(self, identity: str) -> IdentityState:
        self.polled.append(identity)
        return IdentityState(
            identity=identity,
            status=self.status_value,
            verified_for_sending=self.verified,
            dkim_tokens=("tok1", "tok2"),
        )


@pytest.fixture
def ses_identity(monkeypatch):
    """Provider=ses + a fake client behind the production seam."""
    monkeypatch.setenv("RELAY_SENDER_PROVIDER", "ses")
    get_settings.cache_clear()
    fake = FakeIdentityClient()
    monkeypatch.setattr(identity_module, "client_factory", lambda: fake)
    yield fake
    get_settings.cache_clear()


def _tenant_with_address(client, address="outreach@tenant-a.example.com") -> str:
    response = client.post(
        "/internal/tenants/onboard",
        json={
            "name": f"idtenant-{uuid.uuid4().hex[:8]}",
            "sender_from_address": address,
            "source": {
                "name": "s",
                "source_type": "synthetic",
                "terms_allow_use": "yes",
            },
            "campaign": {"name": "c"},
        },
        headers=ADMIN,
    )
    assert response.status_code == 201, response.text
    return response.json()["tenant_id"]


def test_identity_for_scopes():
    assert identity_for("a@mail.example.com", "domain") == "mail.example.com"
    assert identity_for("a@mail.example.com", "address") == "a@mail.example.com"


def test_provision_requires_ses_provider(client, monkeypatch):
    monkeypatch.setenv("RELAY_SENDER_PROVIDER", "none")
    get_settings.cache_clear()
    tenant_id = _tenant_with_address(client)
    response = client.post(
        f"/internal/tenants/{tenant_id}/sender-identity/provision", headers=ADMIN
    )
    get_settings.cache_clear()
    assert response.status_code == 409
    assert "RELAY_SENDER_PROVIDER" in response.json()["detail"]


def test_provision_requires_sender_address(client, ses_identity):
    response = client.post(
        "/tenants", json={"name": f"noaddr-{uuid.uuid4().hex[:6]}"}, headers=ADMIN
    )
    tenant_id = response.json()["id"]
    response = client.post(
        f"/internal/tenants/{tenant_id}/sender-identity/provision", headers=ADMIN
    )
    assert response.status_code == 409
    assert "sender_from_address" in response.json()["detail"]


def test_provision_returns_dns_records_and_audits(client, ses_identity):
    tenant_id = _tenant_with_address(client)
    response = client.post(
        f"/internal/tenants/{tenant_id}/sender-identity/provision", headers=ADMIN
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["identity"] == "tenant-a.example.com"  # domain scope default
    assert body["ses_status"] == "PENDING"
    assert body["sender_identity_verified"] is False
    assert body["dkim_records"] == [
        {
            "type": "CNAME",
            "name": "tok1._domainkey.tenant-a.example.com",
            "value": "tok1.dkim.amazonses.com",
        },
        {
            "type": "CNAME",
            "name": "tok2._domainkey.tenant-a.example.com",
            "value": "tok2.dkim.amazonses.com",
        },
    ]
    assert ses_identity.provisioned == ["tenant-a.example.com"]
    with admin_session() as session:
        actions = session.execute(
            select(AuditLog.action).where(
                AuditLog.tenant_id == uuid.UUID(tenant_id),
                AuditLog.action == "tenant.sender_identity_provision",
            )
        ).all()
        assert len(actions) == 1


def test_sync_flips_attest_once_when_ses_confirms(client, ses_identity):
    tenant_id = _tenant_with_address(client)
    url = f"/internal/tenants/{tenant_id}/sender-identity/sync"

    # Not verified yet → attest stays false.
    response = client.post(url, headers=ADMIN)
    assert response.status_code == 200
    assert response.json()["sender_identity_verified"] is False

    # SES confirms → attest flips, audited as the system actor.
    ses_identity.verified = True
    ses_identity.status_value = "SUCCESS"
    response = client.post(url, headers=ADMIN)
    assert response.json()["sender_identity_verified"] is True
    with admin_session() as session:
        tenant = session.get(Tenant, uuid.UUID(tenant_id))
        assert tenant.sender_identity_verified is True
        attests = session.execute(
            select(AuditLog).where(
                AuditLog.tenant_id == uuid.UUID(tenant_id),
                AuditLog.action == "tenant.attest_sender_identity",
            )
        ).all()
        assert len(attests) == 1

    # Re-sync: idempotent — no second attest audit row.
    response = client.post(url, headers=ADMIN)
    assert response.json()["sender_identity_verified"] is True
    with admin_session() as session:
        attests = session.execute(
            select(AuditLog).where(
                AuditLog.tenant_id == uuid.UUID(tenant_id),
                AuditLog.action == "tenant.attest_sender_identity",
            )
        ).all()
        assert len(attests) == 1


def test_sync_never_unflips_a_verified_attest(client, ses_identity):
    """A transient SES blip must not revoke eligibility on its own."""
    tenant_id = _tenant_with_address(client)
    url = f"/internal/tenants/{tenant_id}/sender-identity/sync"
    ses_identity.verified = True
    client.post(url, headers=ADMIN)
    ses_identity.verified = False  # provider hiccup / eventual consistency
    response = client.post(url, headers=ADMIN)
    assert response.json()["verified_for_sending"] is False
    assert response.json()["sender_identity_verified"] is True


def test_address_scope_provisions_the_mailbox(client, ses_identity):
    tenant_id = _tenant_with_address(client)
    response = client.post(
        f"/internal/tenants/{tenant_id}/sender-identity/provision",
        json={"scope": "address"},
        headers=ADMIN,
    )
    assert response.json()["identity"] == "outreach@tenant-a.example.com"


def test_endpoints_require_admin_token(client, ses_identity):
    tenant_id = _tenant_with_address(client)
    for endpoint in ("provision", "sync"):
        response = client.post(
            f"/internal/tenants/{tenant_id}/sender-identity/{endpoint}"
        )
        assert response.status_code in (401, 403, 422)
