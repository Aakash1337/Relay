"""The simulated sender — records the send; touches nothing external."""

from __future__ import annotations

from relay.db.models import Lead, OutreachDraft, SendJob
from relay.logs import get_logger

log = get_logger(__name__)


class SimulatedSender:
    name = "simulated"

    def send(
        self,
        *,
        job: SendJob,
        draft: OutreachDraft,
        lead: Lead,
        sender_identity: str | None = None,
    ) -> str:
        message_id = f"simulated-{job.id}"
        log.info(
            "simulated send executed",
            send_job_id=str(job.id),
            lead_id=str(job.lead_id),
            message_version=job.message_version,
            provider_message_id=message_id,
        )
        return message_id
