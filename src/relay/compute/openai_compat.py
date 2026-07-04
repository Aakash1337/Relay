"""OpenAI-compatible chat endpoint (Ollama, vLLM, llama.cpp server, …).

Usable on either tier — the tier's meaning comes from the router, not
from which provider serves it. No tool support by construction: a
confused model can produce at most one JSON blob that downstream gates
re-validate.
"""

from __future__ import annotations

import httpx

from relay.compute.base import (
    ComputeConfigError,
    ComputeRequest,
    ComputeResponse,
    ComputeUnavailable,
    parse_json_output,
)
from relay.config import get_settings
from relay.logs import get_logger

log = get_logger(__name__)


class OpenAICompatBackend:
    name = "openai_compat"

    def __init__(self, *, model: str, client: httpx.Client | None = None) -> None:
        settings = get_settings()
        if not model:
            raise ComputeConfigError(
                "a model ID is required for the openai backend "
                "(RELAY_LOCAL_MODEL / RELAY_HOSTED_MODEL)"
            )
        self.model = model
        self._client = client or httpx.Client(
            base_url=settings.openai_compat_base_url.rstrip("/"),
            timeout=settings.compute_timeout_seconds,
            headers={
                # Ollama ignores auth; other servers may require a token.
                "Authorization": (
                    f"Bearer {settings.openai_compat_api_key.get_secret_value()}"
                ),
            },
        )

    def complete(self, request: ComputeRequest) -> ComputeResponse:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "temperature": 0.2,
            "max_tokens": request.max_output_tokens,
            # Honored by Ollama/vLLM/llama.cpp; harmless where ignored.
            "response_format": {"type": "json_object"},
        }
        try:
            resp = self._client.post("/chat/completions", json=body)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ComputeUnavailable(f"openai backend unreachable: {exc}") from exc

        data = resp.json()
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ComputeUnavailable(
                f"openai backend returned unexpected shape: {exc}"
            ) from exc
        usage = data.get("usage") or {}
        output = parse_json_output(text, backend=self.name)
        log.info(
            "openai-compat completion",
            task_type=str(request.task_type),
            model=self.model,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )
        return ComputeResponse(
            output=output,
            backend=self.name,
            model=self.model,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )
