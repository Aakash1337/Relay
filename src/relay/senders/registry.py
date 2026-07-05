"""Sender selection — configuration decides, code fails loudly.

Simulated mode always gets the simulated sender: that pairing is not
configurable, because a 'simulated' job that touched a real provider
would falsify the dry-run guarantee. Real mode requires BOTH the enable
flag AND a configured provider — with neither, real sending remains
structurally absent exactly as in Phase 0.
"""

from __future__ import annotations

import threading

from relay.config import get_settings
from relay.logs import get_logger
from relay.senders.base import DirectSender, RealSendUnavailable
from relay.senders.simulated import SimulatedSender

log = get_logger(__name__)

_cache: dict[str, DirectSender] = {}
#: Construction is guarded: concurrent worker threads must not both build
#: a provider client (boto3 client creation on the shared default session
#: is not thread-safe), and a failure mid-construction must not poison
#: the cache.
_cache_lock = threading.Lock()


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
        with _cache_lock:
            cached = _cache.get(provider)  # double-checked under the lock
            if cached is None:
                if provider == "ses":
                    # The Smartlead enrollment adapter is deliberately
                    # deferred (§6 record) and will NOT appear here:
                    # enrollment providers get their own registry when
                    # built, because they change the send moment.
                    from relay.senders.ses import SESSender

                    cached = SESSender()
                else:
                    # Fail loudly on a provider the Literal was extended
                    # to allow but this registry was never taught to
                    # build — never silently fall through to another.
                    raise RealSendUnavailable(f"unhandled sender provider {provider!r}")
                _cache[provider] = cached
                log.info("real sender ready", provider=provider)
    return cached


def real_sender_status() -> tuple[bool, str]:
    """Provider-neutral readiness: can a real send actually be executed
    with the current configuration? The eligibility gate asks THIS instead
    of reading any provider's private settings, so swapping providers never
    means editing the gate."""
    settings = get_settings()
    if not settings.real_send_enabled:
        return False, "RELAY_REAL_SEND_ENABLED is false"
    provider = settings.sender_provider
    if provider == "none":
        return False, "no sender provider configured (RELAY_SENDER_PROVIDER=none)"
    if provider == "ses":
        from relay.senders.ses import SESSender

        error = SESSender.config_error()
        return (error is None, error or "ready")
    return False, f"unhandled sender provider {provider!r}"


def reset_senders() -> None:
    _cache.clear()
