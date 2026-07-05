"""Sender contracts — the two operational shapes from the §6 record."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from relay.db.models import Campaign, Lead, OutreachDraft, SendJob


class RealSendUnavailable(RuntimeError):
    """Raised whenever anything reaches for a real send that the current
    configuration does not structurally provide."""


@runtime_checkable
class DirectSender(Protocol):
    """Shape 1 — RELAY owns the send moment (SES, SMTP).

    One call = one message. The send-job row is 1:1 with the email, so
    the existing idempotency constraint and one-active-send index guard
    the actual send.
    """

    name: str

    def send(
        self,
        *,
        job: SendJob,
        draft: OutreachDraft,
        lead: Lead,
        sender_identity: str | None = None,
    ) -> str:
        """Execute one send; return the provider message id.

        ``sender_identity`` is the tenant's own from-address (Phase 4
        mailbox/domain ownership) — None falls back to the provider's
        globally configured identity.
        """
        ...  # pragma: no cover


@runtime_checkable
class EnrollmentSender(Protocol):
    """Shape 2 — the provider owns the send moment (Smartlead; deferred).

    INTERFACE ONLY in Phase 1C. Read the idempotency-boundary note in
    docs/decisions/sending-provider.md before implementing:

    - "one active send per lead" must be re-expressed as "one active
      enrollment per lead", enforced in RELAY's DB BEFORE the API call;
    - ``idempotency_key`` guards the *enroll call* — a retried enroll
      must not double-enroll, and provider-side email dedupe must not
      be relied on;
    - the transition to ``sent`` is driven by the provider's *sent
      webhook*, never by the enroll response (which only confirms
      enrollment);
    - crash recovery must detect an already-made enroll call (by key or
      provider query) instead of re-enrolling — the "outcome unknown →
      retry anyway → double-send" trap, now across the API boundary.
    """

    name: str

    def enroll(
        self,
        *,
        lead: Lead,
        campaign: Campaign,
        draft: OutreachDraft,
        idempotency_key: str,
    ) -> str:
        """Enroll the lead; return the provider enrollment id."""
        ...  # pragma: no cover


#: What the worker deals with today (direct shape only in 1C).
Sender = DirectSender
