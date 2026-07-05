"""SQS event poller (Phase 1C) — the no-public-endpoint transport.

SNS→SQS→this worker: SES events land in a queue and RELAY polls it on
the spine's schedule, so nothing needs to be reachable from the
internet during the pilot. Each message body is a full SNS envelope and
goes through exactly the same signature-verified handler as the HTTPS
webhook; a message is deleted from the queue only after successful
processing (failed ones redeliver — the handler is idempotent).

    uv run relay-events            # one drain pass
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any

from relay.config import get_settings
from relay.ingest.ses_events import EventRejected, process_sns_envelope
from relay.logs import get_logger, setup_logging

log = get_logger(__name__)


@dataclass
class PollStats:
    received: int = 0
    processed: int = 0
    rejected: int = 0


def poll_once(*, client: Any | None = None, max_messages: int = 10) -> PollStats:
    settings = get_settings()
    stats = PollStats()
    if not settings.sqs_queue_url:
        log.info("sqs polling disabled (RELAY_SQS_QUEUE_URL unset)")
        return stats
    if client is None:
        if not settings.aws_region:
            raise RuntimeError("RELAY_AWS_REGION must be set for SQS polling")
        import boto3

        client = boto3.client("sqs", region_name=settings.aws_region)

    response = client.receive_message(
        QueueUrl=settings.sqs_queue_url,
        MaxNumberOfMessages=min(max_messages, 10),
        WaitTimeSeconds=0,
    )
    for message in response.get("Messages", []):
        stats.received += 1
        try:
            process_sns_envelope(message.get("Body", ""))
        except EventRejected as exc:
            # Rejected envelopes are deleted too: redelivering a forged
            # or malformed message forever helps no one; it is logged.
            stats.rejected += 1
            log.warning("sqs message rejected", error=str(exc))
        else:
            stats.processed += 1
        client.delete_message(
            QueueUrl=settings.sqs_queue_url,
            ReceiptHandle=message.get("ReceiptHandle", ""),
        )
    if stats.received:
        log.info(
            "sqs poll complete",
            received=stats.received,
            processed=stats.processed,
            rejected=stats.rejected,
        )
    return stats


def main() -> None:
    setup_logging()
    # Make AWS creds in a local .env visible to boto3's credential chain.
    from relay.bootstrap import load_local_dotenv

    load_local_dotenv()
    parser = argparse.ArgumentParser(description="RELAY SES/SNS event poller")
    parser.add_argument("--max-messages", type=int, default=10)
    args = parser.parse_args()
    poll_once(max_messages=args.max_messages)


if __name__ == "__main__":
    main()
