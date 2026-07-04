"""In-memory CRM — the hermetic sync target for dev and tests."""

from __future__ import annotations

from relay.crm.base import CRMLeadSnapshot


class InMemoryCRM:
    name = "memory"

    def __init__(self) -> None:
        self.leads: dict[str, CRMLeadSnapshot] = {}
        self.events: list[tuple[str, str, str]] = []

    def upsert_lead(self, snapshot: CRMLeadSnapshot) -> str:
        self.leads[snapshot.external_ref] = snapshot
        return f"mem-{snapshot.external_ref}"

    def record_event(self, external_ref: str, kind: str, detail: str) -> None:
        self.events.append((external_ref, kind, detail))

    def delete_lead(self, external_ref: str) -> bool:
        return self.leads.pop(external_ref, None) is not None
