"""Adapter contract for CRM targets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class CRMError(Exception):
    """CRM sync failed (network, auth, remote validation)."""


class CRMConfigError(CRMError):
    """The adapter is misconfigured; fails at construction."""


@dataclass(frozen=True)
class CRMLeadSnapshot:
    """What RELAY shares with the CRM — a mirror row, not the source of
    truth. ``external_ref`` is the RELAY lead id and the upsert key."""

    external_ref: str
    tenant_ref: str
    email: str
    state: str
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    company: str | None = None
    fit_score: float | None = None
    dry_run: bool = True


@runtime_checkable
class CRMAdapter(Protocol):
    name: str

    def upsert_lead(self, snapshot: CRMLeadSnapshot) -> str:
        """Create or update the mirror row; returns the CRM-side id."""
        ...  # pragma: no cover

    def record_event(self, external_ref: str, kind: str, detail: str) -> None:
        """Attach an activity/note to the mirrored lead."""
        ...  # pragma: no cover
