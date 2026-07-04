"""Backend selection — configuration decides, code fails loudly.

Each tier maps to exactly one backend chosen by RELAY_COMPUTE_LOCAL_BACKEND
/ RELAY_COMPUTE_HOSTED_BACKEND. Both default to 'offline' so a fresh
checkout is hermetic. There is deliberately NO automatic fallback chain:
if an operator configured a real backend and it cannot be constructed,
that is a deployment error to surface, not a reason to silently answer
with a weaker (or fake) model.
"""

from __future__ import annotations

from relay.compute.base import ComputeBackend, ComputeConfigError
from relay.config import get_settings
from relay.logs import get_logger
from relay.routing.router import ComputeTier

log = get_logger(__name__)

_cache: dict[ComputeTier, ComputeBackend] = {}


def _build(tier: ComputeTier) -> ComputeBackend:
    settings = get_settings()
    choice = (
        settings.compute_local_backend
        if tier is ComputeTier.LOCAL
        else settings.compute_hosted_backend
    )
    if choice == "offline":
        from relay.compute.offline import OfflineBackend

        return OfflineBackend()
    if choice == "openai" and tier is ComputeTier.LOCAL:
        from relay.compute.local_openai import LocalOpenAIBackend

        return LocalOpenAIBackend()
    if choice == "anthropic" and tier is ComputeTier.HOSTED:
        from relay.compute.hosted_anthropic import HostedAnthropicBackend

        return HostedAnthropicBackend()
    raise ComputeConfigError(f"backend '{choice}' is not valid for tier '{tier}'")


def backend_for(tier: ComputeTier) -> ComputeBackend:
    """Return the (cached) backend for a tier, constructing it on first use."""
    backend = _cache.get(tier)
    if backend is None:
        backend = _build(tier)
        _cache[tier] = backend
        log.info("compute backend ready", tier=str(tier), backend=backend.name)
    return backend


def reset_backends() -> None:
    """Drop cached backends (tests / settings reload)."""
    _cache.clear()
