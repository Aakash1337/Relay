"""Amazon SES sender (Phase 1C pilot — sandbox, self-to-self only).

Direct-send shape: RELAY owns the send moment; delivery outcome arrives
asynchronously via SNS (relay.ingest.ses_events). In sandbox mode SES
itself refuses any recipient that is not a verified identity, which
makes emailing a stranger structurally impossible during the pilot —
that property comes from AWS, on top of every RELAY gate.

Defense in depth at the last hop: the recipient handed to SES is
re-derived from the LEAD row and cross-checked against the job's frozen
recipient hash. A job whose hash does not match its lead's address was
already impossible to insert (DB trigger); this catches the same class
of tampering at the final boundary too.
"""

from __future__ import annotations

from typing import Any

from relay.config import get_settings
from relay.db.models import Lead, OutreachDraft, SendJob
from relay.hashing import hash_email
from relay.logs import get_logger
from relay.senders.base import RealSendUnavailable

log = get_logger(__name__)


class SESSender:
    name = "ses"

    @staticmethod
    def config_error() -> str | None:
        """Provider-neutral readiness probe (no client construction, no
        network): returns a reason string if this sender could not be
        built and used, else None. The eligibility gate consults this via
        the registry instead of reading SES-specific settings directly."""
        settings = get_settings()
        if not settings.ses_from_address:
            return "RELAY_SES_FROM not set"
        if not settings.aws_region:
            return "AWS_REGION not set"
        return None

    def __init__(self, *, client: Any | None = None) -> None:
        settings = get_settings()
        # from_address is used by send() itself, so it is required even with
        # an injected client; region is only needed to build the real one.
        if not settings.ses_from_address:
            raise RealSendUnavailable(
                "RELAY_SES_FROM must be set to use the ses sender"
            )
        if client is None:
            if not settings.aws_region:
                raise RealSendUnavailable(
                    "AWS_REGION must be set to use the ses sender"
                )
            import boto3  # deferred: not needed when the sender is unused

            client = boto3.client("sesv2", region_name=settings.aws_region)
        self._client = client
        self._from_address = settings.ses_from_address
        self._configuration_set = settings.ses_configuration_set
        self._unsubscribe_mailto = settings.unsubscribe_mailto
        self._unsubscribe_url = settings.unsubscribe_url
        self._allowlist = frozenset(
            hash_email(a) for a in settings.pilot_recipient_addresses()
        )

    def send(self, *, job: SendJob, draft: OutreachDraft, lead: Lead) -> str:
        # Last-hop cross-check: the address we are about to hand to the
        # provider must hash to the job's frozen recipient identity.
        if hash_email(lead.email) != job.recipient_email_hash:
            raise RealSendUnavailable(
                "refusing send: lead address does not match the job's "
                "frozen recipient hash"
            )
        # Last-hop backstop of the §6 pilot allowlist (also gated in
        # eligibility): a real send may leave only for an allowlisted inbox.
        # Fail-closed — an empty allowlist refuses every real send.
        if job.recipient_email_hash not in self._allowlist:
            raise RealSendUnavailable(
                "refusing send: recipient is not on the pilot allowlist "
                "(RELAY_PILOT_RECIPIENTS)"
            )

        # List-Unsubscribe can carry a mailto and/or an https URL. RFC 8058
        # one-click (List-Unsubscribe-Post) is only valid when an https URL
        # is present — a mailto cannot honor a one-click POST — so we
        # advertise One-Click ONLY when a URL is configured.
        targets = []
        if self._unsubscribe_url:
            targets.append(f"<{self._unsubscribe_url}>")
        if self._unsubscribe_mailto:
            targets.append(f"<mailto:{self._unsubscribe_mailto}>")
        headers = []
        if targets:
            headers.append({"Name": "List-Unsubscribe", "Value": ", ".join(targets)})
        if self._unsubscribe_url:
            headers.append(
                {
                    "Name": "List-Unsubscribe-Post",
                    "Value": "List-Unsubscribe=One-Click",
                }
            )

        request: dict[str, Any] = {
            "FromEmailAddress": self._from_address,
            "Destination": {"ToAddresses": [lead.email]},
            "Content": {
                "Simple": {
                    "Subject": {"Data": draft.subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": draft.body, "Charset": "UTF-8"}},
                    **({"Headers": headers} if headers else {}),
                }
            },
        }
        if self._configuration_set:
            request["ConfigurationSetName"] = self._configuration_set

        response = self._client.send_email(**request)
        message_id = response["MessageId"]
        log.info(
            "ses send executed",
            send_job_id=str(job.id),
            lead_id=str(job.lead_id),
            recipient_hash=job.recipient_email_hash,
            provider_message_id=message_id,
        )
        return message_id
