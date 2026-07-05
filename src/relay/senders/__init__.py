"""Senders — the last hop of the send path, behind a provider seam.

Since Phase 1C the sender is a config-selected provider (like compute
and CRM): ``RELAY_SENDER_PROVIDER`` picks the implementation, there is
no silent fallback, and swapping providers never touches the gates, the
state machine, or any call site. Two operational shapes exist (§6
decision record, docs/decisions/sending-provider.md):

- **direct send** (SES): RELAY emits one message per send job;
- **campaign enrollment** (Smartlead, deferred): RELAY hands a lead to
  the provider's campaign; outcomes arrive via webhook. The interface
  exists now; the adapter is deliberately NOT built until real-prospect
  production (building it unusable would be scaffolding theater).

Default posture is unchanged from Phase 0: with no provider configured,
a real send is structurally absent — reaching for one raises.
"""

from relay.senders.base import (
    DirectSender,
    EnrollmentSender,
    RealSendUnavailable,
    Sender,
)
from relay.senders.registry import reset_senders, sender_for_mode
from relay.senders.simulated import SimulatedSender

__all__ = [
    "DirectSender",
    "EnrollmentSender",
    "RealSendUnavailable",
    "Sender",
    "SimulatedSender",
    "reset_senders",
    "sender_for_mode",
]
