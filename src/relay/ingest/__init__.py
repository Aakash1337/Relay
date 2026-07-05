"""Provider event ingestion (Phase 1C): SES-via-SNS → RELAY machinery.

Bounces and complaints from the provider land in the SAME suppression
and state machinery everything else uses — ingestion translates and
authenticates; it never gets its own side-channel writes.
"""

from relay.ingest.ses_events import process_sns_envelope

__all__ = ["process_sns_envelope"]
