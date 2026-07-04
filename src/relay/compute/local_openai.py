"""Local tier — any OpenAI-compatible chat endpoint (Ollama, vLLM, …).

Cheap bounded work only: the router never sends tool-calling or
cascade-risk tasks here (§8), and this client has no tool support at
all — a compromised or confused local model can produce at most one
JSON blob that downstream gates re-validate.
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


class LocalOpenAIBackend:
    name = "local_openai"

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        settings = get_settings()
        if not settings.local_openai_model:
            raise ComputeConfigError(
                "RELAY_LOCAL_OPENAI_MODEL must be set to use the local backend"
            )
        self.model = settings.local_openai_model
        self._client = client or httpx.Client(
            base_url=settings.local_openai_base_url.rstrip("/"),
            timeout=settings.compute_timeout_seconds,
            headers={
                # Ollama ignores auth; other servers may require a token.
                "Authorization": (
                    f"Bearer {settings.local_openai_api_key.get_secret_value()}"
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
            raise ComputeUnavailable(f"local backend unreachable: {exc}") from exc

        data = resp.json()
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ComputeUnavailable(
                f"local backend returned unexpected shape: {exc}"
            ) from exc
        usage = data.get("usage") or {}
        output = parse_json_output(text, backend=self.name)
        log.info(
            "local completion",
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
