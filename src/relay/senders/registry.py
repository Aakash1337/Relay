"""Sender selection — configuration decides, code fails loudly.

Simulated mode always gets the simulated sender: that pairing is not
configurable, because a 'simulated' job that touched a real provider
would falsify the dry-run guarantee. Real mode requires BOTH the enable
flag AND a configured provider — with neither, real sending remains
structurally absent exactly as in Phase 0.
"""

from __future__ import annotations

from relay.config import get_settings
from relay.logs import get_logger
from relay.senders.base import DirectSender, RealSendUnavailable
from relay.senders.simulated import SimulatedSender

log = get_logger(__name__)

_cache: dict[str, DirectSender] = {}


def sender_for_mode(mode: str) -> DirectSender:
    if mode == "simulated":
        return SimulatedSender()
    if mode != "real":
        raise ValueError(f"unknown send mode: {mode!r}")

    settings = get_settings()
    if not settings.real_send_enabled:
        raise RealSendUnavailable(
            "real sending is disabled by configuration (RELAY_REAL_SEND_ENABLED=false)"
        )
    provider = settings.sender_provider
    if provider == "none":
        raise RealSendUnavailable(
            "no real sender is configured (RELAY_SENDER_PROVIDER=none): "
            "real sending is structurally absent, as in Phase 0"
        )
    cached = _cache.get(provider)
    if cached is None:
        # 'ses' — the Literal type admits nothing else. The Smartlead
        # enrollment adapter is deliberately deferred (§6 record) and
        # will NOT appear here: enrollment providers get their own
        # registry when built, because they change the send moment.
        from relay.senders.ses import SESSender

        cached = SESSender()
        _cache[provider] = cached
        log.info("real sender ready", provider=provider)
    return cached


def reset_senders() -> None:
    _cache.clear()
