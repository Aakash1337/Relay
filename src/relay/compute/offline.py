"""The offline backend — deterministic reasoning stand-in, zero network.

This is the default for both tiers in dev and CI: the pipeline, the
guardrails, and every safety gate run for real while the "reasoning" is
a hermetic function of its input. Outputs are deliberately input-
sensitive (hash-derived scores, keyword triage, template copy) so
synthetic dry-runs exercise branching — different prospects take
different paths — while staying byte-for-byte reproducible.

Being a dumb function, it is also structurally immune to prompt
injection: untrusted text is only ever substring-matched, never
interpreted.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from relay.compute.base import ComputeRequest, ComputeResponse
from relay.compute.prompting import UNTRUSTED_KEY

#: Phrases that triage a simulated reply as an opt-out. Over-matching is
#: the safe direction — a false 'unsubscribed' only suppresses harder.
_OPT_OUT_PHRASES = (
    "unsubscribe",
    "opt out",
    "opt-out",
    "remove me",
    "stop emailing",
    "stop contacting",
    "do not contact",
    "take me off",
)
_DECLINE_PHRASES = (
    "not interested",
    "no thanks",
    "no thank you",
    "not a fit",
    "not right now",
    "we already have",
    "please pass",
)


def _stable_unit(payload: dict[str, Any], salt: str) -> float:
    """Deterministic pseudo-value in [0, 1) derived from the payload."""
    blob = json.dumps(payload, sort_keys=True, default=str) + salt
    digest = hashlib.sha256(blob.encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _untrusted_text(payload: dict[str, Any]) -> str:
    parts = payload.get(UNTRUSTED_KEY) or {}
    return " ".join(str(v) for _, v in sorted(parts.items())).lower()


class OfflineBackend:
    name = "offline"
    model = "offline-deterministic"

    def complete(self, request: ComputeRequest) -> ComputeResponse:
        handler = getattr(self, f"_do_{request.task_type.value}", None)
        output = (
            handler(request.payload)
            if handler
            else dict.fromkeys(request.output_fields)
        )
        return ComputeResponse(output=output, backend=self.name, model=self.model)

    # ── Per-task deterministic behaviors ────────────────────────────────────

    def _do_enrichment(self, payload: dict[str, Any]) -> dict[str, Any]:
        company = payload.get("company", "the company")
        return {
            "company_summary": (
                f"{company} is a synthetic prospect generated for dry-run "
                "testing; no real enrichment was performed."
            ),
            "signals": ["synthetic"],
        }

    def _do_field_extraction(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"fields": {}}

    def _do_classification(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"label": "pass", "confidence": 1.0}

    def _do_tagging(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"tags": ["synthetic"]}

    def _do_summarization(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"summary": "Deterministic offline summary of the provided data."}

    def _do_fit_scoring(self, payload: dict[str, Any]) -> dict[str, Any]:
        # 0.35–0.99, stable per prospect: some synthetic leads disqualify.
        score = round(0.35 + _stable_unit(payload, "fit") * 0.64, 2)
        return {
            "fit_score": score,
            "rationale": "Hash-derived synthetic score (offline backend).",
        }

    def _do_reply_triage(self, payload: dict[str, Any]) -> dict[str, Any]:
        text = _untrusted_text(payload)
        if any(p in text for p in _OPT_OUT_PHRASES):
            category = "unsubscribed"
        elif any(p in text for p in _DECLINE_PHRASES):
            category = "not_interested"
        else:
            category = "interested"
        return {"category": category, "confidence": 1.0}

    def _do_outreach_copy(self, payload: dict[str, Any]) -> dict[str, Any]:
        first_name = payload.get("first_name", "there")
        company = payload.get("company", "your team")
        offer = payload.get("offer", "what we're building")
        return {
            "subject": f"Quick question for {company}"[:60],
            "body": (
                f"Hi {first_name},\n\n"
                f"I'm reaching out because {offer} may be relevant to "
                f"{company}. If it's useful I can share a short overview — "
                "and if not, tell me and I won't follow up.\n\n"
                "[synthetic draft — offline backend]"
            ),
            "personalization_sources": {
                "first_name": "lead.first_name",
                "company": "lead.company",
            },
        }

    def _do_orchestration(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"plan": []}

    def _do_sensitive(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"result": None, "note": "offline backend declines sensitive tasks"}
