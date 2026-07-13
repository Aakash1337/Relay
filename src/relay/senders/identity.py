"""Per-tenant SES sender-identity automation (§6 gap-fill).

Phase 4 added per-tenant ``sender_from_address`` plus a manual operator
attest (``sender_identity_verified``) gating real sends. This module
closes the loop with the provider: *provision* creates the identity at
SES (idempotent) and returns the DNS records the tenant must publish;
*status* polls verification so the attest can flip automatically the
moment AWS confirms — no console clicking per tenant.

What stays manual, on purpose: publishing the DNS records (the tenant
owns their zone) and the attest endpoint itself, which remains the
escape hatch for non-SES providers. Nothing here can send; verification
state only ever loosens ONE of the seventeen send checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from relay.config import get_settings
from relay.logs import get_logger

log = get_logger(__name__)


class IdentityUnavailable(Exception):
    """Identity operations impossible as configured (wrong provider…)."""


@dataclass(frozen=True)
class IdentityState:
    identity: str
    #: SES DKIM/verification status: PENDING | SUCCESS | FAILED |
    #: TEMPORARY_FAILURE | NOT_STARTED — SUCCESS means verified.
    status: str
    verified_for_sending: bool
    dkim_tokens: tuple[str, ...] = field(default_factory=tuple)

    def dkim_records(self) -> list[dict[str, str]]:
        """The CNAMEs the identity's DNS zone needs (domain identities)."""
        return [
            {
                "type": "CNAME",
                "name": f"{token}._domainkey.{self.identity}",
                "value": f"{token}.dkim.amazonses.com",
            }
            for token in self.dkim_tokens
        ]


class SesIdentityClient:
    """Thin SESv2 identity wrapper, same construction rules as the sender
    (deferred boto3 import, injectable client for tests)."""

    def __init__(self, *, client: Any | None = None) -> None:
        if client is None:
            import boto3  # deferred: not needed when identities are unused

            client = boto3.client(
                "sesv2", region_name=get_settings().aws_region or None
            )
        self._client = client

    def provision(self, identity: str) -> IdentityState:
        """Create the identity if absent; either way, report its state."""
        try:
            self._client.create_email_identity(EmailIdentity=identity)
            log.info("ses identity created", identity=identity)
        except Exception as exc:  # AlreadyExists is fine — idempotent.
            if type(exc).__name__ != "AlreadyExistsException":
                raise
        return self.status(identity)

    def status(self, identity: str) -> IdentityState:
        response = self._client.get_email_identity(EmailIdentity=identity)
        dkim = response.get("DkimAttributes") or {}
        return IdentityState(
            identity=identity,
            status=str(dkim.get("Status", "NOT_STARTED")),
            verified_for_sending=bool(response.get("VerifiedForSendingStatus")),
            dkim_tokens=tuple(dkim.get("Tokens") or ()),
        )


#: Test seam: monkeypatch this factory; production always builds the real
#: client. Same shape as the sender registry's construction rule — the
#: provider must be SES for identity automation to mean anything.
def _default_factory() -> SesIdentityClient:
    return SesIdentityClient()


client_factory = _default_factory


def identity_client() -> SesIdentityClient:
    if get_settings().sender_provider != "ses":
        raise IdentityUnavailable(
            "sender-identity automation requires RELAY_SENDER_PROVIDER=ses "
            f"(configured: {get_settings().sender_provider!r})"
        )
    return client_factory()


def identity_for(sender_from_address: str, scope: str) -> str:
    """The SES identity string for a tenant address, by scope.

    'domain' (default) verifies the whole sending domain — DKIM CNAMEs,
    one DNS setup covers every mailbox at it. 'address' verifies the
    single mailbox — SES emails a confirmation link to it instead.
    """
    if scope == "address":
        return sender_from_address
    return sender_from_address.rsplit("@", 1)[-1]
