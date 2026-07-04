"""Senders — the last hop of the send path.

Phase 0 truth: **no real send path exists.** ``RealSender`` is a named
seam that refuses to construct. This is deliberate (roadmap Phase 0 exit
gate: "no code path can send while dry_run is set" — and in Phase 0, no
code path can send at all). The layers, outermost first:

1. campaigns/leads default to ``dry_run = true`` (immutable after insert);
2. the DB trigger rejects any ``mode='real'`` job for a dry-run lead or
   campaign;
3. the eligibility gate fails real mode on seven independent checks;
4. ``RELAY_REAL_SEND_ENABLED`` defaults to false;
5. this module raises before any provider could even be contacted.

Phase 1C replaces layer 5 with a Mailpit/SMTP sender behind the same
interface — after the deliverability and provider gates pass.
"""

from __future__ import annotations

from typing import Protocol

from relay.config import get_settings
from relay.db.models import OutreachDraft, SendJob
from relay.logs import get_logger

log = get_logger(__name__)


class RealSendUnavailable(RuntimeError):
    """Raised whenever anything reaches for a real send in Phase 0."""


class Sender(Protocol):
    def send(self, *, job: SendJob, draft: OutreachDraft) -> str:
        """Execute the send; return a provider message id."""
        ...


class SimulatedSender:
    """Records the send; touches no network, no SMTP, nothing external."""

    def send(self, *, job: SendJob, draft: OutreachDraft) -> str:
        message_id = f"simulated-{job.id}"
        log.info(
            "simulated send executed",
            send_job_id=str(job.id),
            lead_id=str(job.lead_id),
            message_version=job.message_version,
            provider_message_id=message_id,
        )
        return message_id


class RealSender:
    """Phase 0: structurally absent. Construction is refusal."""

    def __init__(self) -> None:
        raise RealSendUnavailable(
            "Phase 0 has no real send path. Real sending requires the "
            "deliverability, suppression, provider-approval, and audit "
            "gates of Phase 1C (development roadmap)."
        )

    def send(self, *, job: SendJob, draft: OutreachDraft) -> str:
        raise RealSendUnavailable("unreachable")  # pragma: no cover


def sender_for_mode(mode: str) -> Sender:
    if mode == "simulated":
        return SimulatedSender()
    if mode == "real":
        if not get_settings().real_send_enabled:
            raise RealSendUnavailable(
                "real sending is disabled by configuration "
                "(RELAY_REAL_SEND_ENABLED=false)"
            )
        return RealSender()
    raise ValueError(f"unknown send mode: {mode!r}")
