"""Backend selection — configuration decides, code fails loudly.

Each tier maps to one provider (RELAY_COMPUTE_{LOCAL,HOSTED}_BACKEND)
plus one model (RELAY_{LOCAL,HOSTED}_MODEL), independently. Any provider
can serve either tier — today's pairing (a Gemini orchestrator over a
Gemma workhorse) and tomorrow's (say, a Claude orchestrator) differ only
in .env. Both default to 'offline' so a fresh checkout is hermetic.

There is deliberately NO automatic fallback chain: if an operator
configured a real backend and it cannot be constructed, that is a
deployment error to surface, not a reason to silently answer with a
weaker (or fake) model.
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
    if tier is ComputeTier.LOCAL:
        choice, model, model_var = (
            settings.compute_local_backend,
            settings.local_model,
            "RELAY_LOCAL_MODEL",
        )
    else:
        choice, model, model_var = (
            settings.compute_hosted_backend,
            settings.hosted_model,
            "RELAY_HOSTED_MODEL",
        )

    if choice == "offline":
        from relay.compute.offline import OfflineBackend

        return OfflineBackend()
    if not model:
        raise ComputeConfigError(
            f"{model_var} must be set to use the '{choice}' backend on the {tier} tier"
        )
    if choice == "openai":
        from relay.compute.openai_compat import OpenAICompatBackend

        return OpenAICompatBackend(model=model)
    if choice == "google":
        from relay.compute.google_api import GoogleGeminiBackend

        return GoogleGeminiBackend(model=model)
    # "anthropic" — the Literal type admits nothing else.
    from relay.compute.anthropic_api import AnthropicBackend

    return AnthropicBackend(model=model)


def backend_for(tier: ComputeTier) -> ComputeBackend:
    """Return the (cached) backend for a tier, constructing it on first use."""
    backend = _cache.get(tier)
    if backend is None:
        backend = _build(tier)
        _cache[tier] = backend
        log.info(
            "compute backend ready",
            tier=str(tier),
            backend=backend.name,
            model=getattr(backend, "model", None),
        )
    return backend


def reset_backends() -> None:
    """Drop cached backends (tests / settings reload)."""
    _cache.clear()
