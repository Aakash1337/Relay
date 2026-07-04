"""Google Gemini API backend — serves both Gemini and Gemma models.

One backend, either tier: point the hosted tier at a Gemini model (the
orchestrator) and the local tier at a Gemma model (the workhorse) and
both run over the same API key. Model quirks are handled per family:

- Gemma models on this API accept no ``systemInstruction`` and no JSON
  response mode, so the system prompt is prepended to the user turn and
  JSON is enforced by parsing (same tolerant parser as every backend);
- Gemini 3.x models get an explicit thinking level, raised on
  extended-reasoning routes.

Safety blocks (``promptFeedback.blockReason`` / SAFETY finish reasons)
surface as ComputeRefused and are never retried.
"""

from __future__ import annotations

import httpx

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

_REFUSAL_FINISH_REASONS = {
    "SAFETY",
    "PROHIBITED_CONTENT",
    "BLOCKLIST",
    "RECITATION",
    "SPII",
}


class GoogleGeminiBackend:
    name = "google"

    def __init__(self, *, model: str, client: httpx.Client | None = None) -> None:
        settings = get_settings()
        if not model:
            raise ComputeConfigError(
                "a model ID is required for the google backend "
                "(RELAY_LOCAL_MODEL / RELAY_HOSTED_MODEL)"
            )
        self.model = model
        if client is None:
            key = settings.google_api_key
            if key is None or not key.get_secret_value():
                raise ComputeConfigError(
                    "RELAY_GOOGLE_API_KEY must be set to use the google backend"
                )
            client = httpx.Client(
                base_url=settings.google_base_url.rstrip("/"),
                timeout=settings.compute_timeout_seconds,
                headers={"x-goog-api-key": key.get_secret_value()},
            )
        self._client = client

    @property
    def _is_gemma(self) -> bool:
        return self.model.lower().startswith("gemma")

    def _request_body(self, request: ComputeRequest) -> dict:
        generation: dict = {
            "temperature": 0.2,
            "maxOutputTokens": request.max_output_tokens,
        }
        if self._is_gemma:
            # No systemInstruction / JSON mode on Gemma via this API: the
            # §11 rules ride at the top of the single user turn instead.
            body: dict = {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": f"{request.system}\n\n{request.user}"}],
                    }
                ]
            }
        else:
            generation["responseMimeType"] = "application/json"
            if self.model.lower().startswith("gemini-3"):
                generation["thinkingConfig"] = {
                    "thinkingLevel": "high" if request.extended_reasoning else "low"
                }
            body = {
                "systemInstruction": {"parts": [{"text": request.system}]},
                "contents": [{"role": "user", "parts": [{"text": request.user}]}],
            }
        body["generationConfig"] = generation
        return body

    def complete(self, request: ComputeRequest) -> ComputeResponse:
        try:
            resp = self._client.post(
                f"/models/{self.model}:generateContent",
                json=self._request_body(request),
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code in (429, 500, 502, 503, 504):
                raise ComputeUnavailable(
                    f"google backend transient error {code}"
                ) from exc
            raise ComputeConfigError(
                f"google backend rejected request ({code}): {exc.response.text[:300]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ComputeUnavailable(f"google backend unreachable: {exc}") from exc

        data = resp.json()
        feedback = data.get("promptFeedback") or {}
        if feedback.get("blockReason"):
            raise ComputeRefused(
                f"google model blocked task {request.task_type}: "
                f"{feedback['blockReason']}"
            )
        candidates = data.get("candidates") or []
        if not candidates:
            raise ComputeRefused(
                f"google model returned no candidates for {request.task_type}"
            )
        candidate = candidates[0]
        finish = candidate.get("finishReason", "")
        if finish in _REFUSAL_FINISH_REASONS:
            raise ComputeRefused(
                f"google model declined task {request.task_type}: {finish}"
            )
        parts = (candidate.get("content") or {}).get("parts") or []
        # Thinking models interleave reasoning parts marked thought=true;
        # only the answer parts are output.
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        output = parse_json_output(text, backend=self.name)
        usage = data.get("usageMetadata") or {}
        log.info(
            "google completion",
            task_type=str(request.task_type),
            model=self.model,
            finish_reason=finish,
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=usage.get("candidatesTokenCount"),
        )
        return ComputeResponse(
            output=output,
            backend=self.name,
            model=self.model,
            input_tokens=usage.get("promptTokenCount"),
            output_tokens=usage.get("candidatesTokenCount"),
        )
