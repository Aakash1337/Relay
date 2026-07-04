"""Backend contract shared by every compute tier.

A backend receives a fully built :class:`ComputeRequest` (the prompt
scaffolding in :mod:`relay.compute.prompting` has already wrapped all
untrusted text) and returns a :class:`ComputeResponse` whose ``output``
is a parsed JSON object. Backends never see raw prospect rows and never
talk to the database — they are pure text-in / JSON-out.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from relay.routing.router import TaskType


class ComputeError(Exception):
    """Base class for compute-layer failures."""


class ComputeConfigError(ComputeError):
    """The backend is misconfigured (missing key, model, endpoint).

    Raised at construction time so a bad deployment fails on startup,
    not mid-pipeline.
    """


class ComputeUnavailable(ComputeError):
    """The backend could not be reached or errored transiently (retryable)."""


class ComputeRefused(ComputeError):
    """The hosted model declined the request (safety refusal).

    Not retryable with the same input; the pipeline parks the lead in an
    error state for human review rather than retrying into the refusal.
    """


class ComputeOutputInvalid(ComputeError):
    """The backend answered, but not with the JSON shape the task needs."""


@dataclass(frozen=True)
class ComputeRequest:
    """One reasoning task, fully assembled and safe to hand to any backend."""

    task_type: TaskType
    #: System prompt: role, §11 injection rules, output contract.
    system: str
    #: User message: task instruction + trusted context + wrapped untrusted data.
    user: str
    #: Required top-level keys of the JSON output (name → description).
    output_fields: dict[str, str] = field(default_factory=dict)
    #: The structured payload the prompt was built from. Prompt-free
    #: backends (offline) read this instead of parsing prose.
    payload: dict[str, Any] = field(default_factory=dict)
    extended_reasoning: bool = False
    max_output_tokens: int = 1024


@dataclass(frozen=True)
class ComputeResponse:
    output: dict[str, Any]
    backend: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


@runtime_checkable
class ComputeBackend(Protocol):
    """What every tier implementation must provide."""

    #: Stable identifier recorded in logs and TaskResults.
    name: str

    def complete(self, request: ComputeRequest) -> ComputeResponse:
        """Run one task to completion. Raises ComputeError subclasses."""
        ...  # pragma: no cover


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json_output(text: str, *, backend: str) -> dict[str, Any]:
    """Parse a model reply that is supposed to be a single JSON object.

    Tolerates the two failure modes worth tolerating (code fences and
    leading/trailing prose around one object); anything else raises
    :class:`ComputeOutputInvalid` — we do not guess at meaning.
    """
    candidate = _FENCE_RE.sub("", text).strip()
    if not candidate.startswith("{"):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise ComputeOutputInvalid(f"{backend}: reply contains no JSON object")
        candidate = candidate[start : end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ComputeOutputInvalid(f"{backend}: invalid JSON in reply: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ComputeOutputInvalid(f"{backend}: reply JSON is not an object")
    return parsed


def require_fields(
    output: dict[str, Any], fields_: dict[str, str], *, backend: str
) -> None:
    """Reject an output that is missing any contracted top-level key."""
    missing = [k for k in fields_ if k not in output]
    if missing:
        raise ComputeOutputInvalid(
            f"{backend}: output missing required fields {missing}"
        )
