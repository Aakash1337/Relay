"""Compute backends — the two-tier reasoning layer made real (Phase 1A).

The routing seam (`relay.routing`) decides *which tier* runs a task; this
package supplies *what actually runs there*:

- ``OfflineBackend`` — deterministic, hermetic, no network. The default
  everywhere (dev, CI) so the pipeline runs with zero external services.
- ``LocalOpenAIBackend`` — any OpenAI-compatible endpoint (Ollama, vLLM,
  llama.cpp server) for the cheap bounded local tier.
- ``HostedAnthropicBackend`` — the Claude API for the hosted tier, where
  being wrong cascades.

Backends are selected by configuration only (`RELAY_COMPUTE_*`); there is
no silent fallback from a configured backend to a weaker one — a
misconfigured or unreachable backend fails loudly instead of quietly
degrading the reasoning tier.
"""

from relay.compute.base import (
    ComputeBackend,
    ComputeConfigError,
    ComputeError,
    ComputeOutputInvalid,
    ComputeRefused,
    ComputeRequest,
    ComputeResponse,
    ComputeUnavailable,
)
from relay.compute.registry import backend_for, reset_backends

__all__ = [
    "ComputeBackend",
    "ComputeConfigError",
    "ComputeError",
    "ComputeOutputInvalid",
    "ComputeRefused",
    "ComputeRequest",
    "ComputeResponse",
    "ComputeUnavailable",
    "backend_for",
    "reset_backends",
]
