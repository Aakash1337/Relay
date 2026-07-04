"""Hosted tier — the Claude API, for tasks where being wrong cascades.

The model ID is deployment configuration (RELAY_HOSTED_MODEL), not code:
model choice is an operational decision with cost and capability
consequences, and pinning it here would rot. Adaptive thinking is always
requested; extended-reasoning routes additionally raise the effort level.
"""

from __future__ import annotations

from typing import Any

from relay.compute.base import (
    ComputeConfigError,
    ComputeRefused,
    ComputeRequest,
    ComputeResponse,
    ComputeUnavailable,
    parse_json_output,
)
from relay.config import get_settings
from relay.logs import get_logger

log = get_logger(__name__)


class HostedAnthropicBackend:
    name = "hosted_anthropic"

    def __init__(self, *, client: Any | None = None) -> None:
        settings = get_settings()
        if not settings.hosted_model:
            raise ComputeConfigError(
                "RELAY_HOSTED_MODEL must be set to use the hosted backend"
            )
        self.model = settings.hosted_model
        if client is None:
            if settings.anthropic_api_key is None:
                raise ComputeConfigError(
                    "RELAY_ANTHROPIC_API_KEY must be set to use the hosted backend"
                )
            import anthropic  # deferred: not needed when backend is unused

            client = anthropic.Anthropic(
                api_key=settings.anthropic_api_key.get_secret_value(),
                timeout=settings.compute_timeout_seconds,
                max_retries=2,
            )
        self._client = client

    def complete(self, request: ComputeRequest) -> ComputeResponse:
        import anthropic

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_output_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": request.user}],
            "thinking": {"type": "adaptive"},
        }
        if request.extended_reasoning:
            kwargs["output_config"] = {"effort": "high"}

        try:
            message = self._client.messages.create(**kwargs)
        except anthropic.APIConnectionError as exc:
            raise ComputeUnavailable(f"hosted backend unreachable: {exc}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code in (429, 500, 502, 503, 504, 529):
                raise ComputeUnavailable(
                    f"hosted backend transient error {exc.status_code}"
                ) from exc
            raise ComputeConfigError(
                f"hosted backend rejected request ({exc.status_code}): {exc.message}"
            ) from exc

        if message.stop_reason == "refusal":
            # Do not retry into a safety refusal; park for human review.
            raise ComputeRefused(f"hosted model declined task {request.task_type}")

        text = "".join(block.text for block in message.content if block.type == "text")
        output = parse_json_output(text, backend=self.name)
        usage = message.usage
        log.info(
            "hosted completion",
            task_type=str(request.task_type),
            model=self.model,
            stop_reason=message.stop_reason,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
        return ComputeResponse(
            output=output,
            backend=self.name,
            model=self.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
