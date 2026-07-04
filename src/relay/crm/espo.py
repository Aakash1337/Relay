"""EspoCRM adapter — the Phase 1A external sync target.

Minimal by intent: upsert a Lead entity keyed by RELAY's lead id (stored
in an Espo field), and attach Notes for events. Auth is Espo's API-key
header. Everything is config; nothing here is required for the pipeline
to run (sync is best-effort by contract).
"""

from __future__ import annotations

import httpx

from relay.config import get_settings
from relay.crm.base import CRMConfigError, CRMError, CRMLeadSnapshot
from relay.logs import get_logger

log = get_logger(__name__)


class EspoCRM:
    name = "espo"

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        settings = get_settings()
        if client is None:
            if not settings.espo_base_url:
                raise CRMConfigError("RELAY_ESPO_BASE_URL must be set for espo CRM")
            if settings.espo_api_key is None:
                raise CRMConfigError("RELAY_ESPO_API_KEY must be set for espo CRM")
            client = httpx.Client(
                base_url=settings.espo_base_url.rstrip("/") + "/api/v1",
                timeout=15.0,
                headers={"X-Api-Key": settings.espo_api_key.get_secret_value()},
            )
        self._client = client

    def _find(self, external_ref: str) -> str | None:
        try:
            resp = self._client.get(
                "/Lead",
                params={
                    "where[0][type]": "equals",
                    "where[0][attribute]": "description",
                    "where[0][value]": f"relay:{external_ref}",
                    "maxSize": 1,
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise CRMError(f"espo lookup failed: {exc}") from exc
        found = resp.json().get("list") or []
        return found[0]["id"] if found else None

    def upsert_lead(self, snapshot: CRMLeadSnapshot) -> str:
        payload = {
            "firstName": snapshot.first_name or "",
            "lastName": snapshot.last_name or "(unknown)",
            "emailAddress": snapshot.email,
            "title": snapshot.title or "",
            "accountName": snapshot.company or "",
            "status": "New",
            # The upsert key lives in description — no custom-field setup
            # required on a stock Espo instance.
            "description": f"relay:{snapshot.external_ref}",
        }
        try:
            espo_id = self._find(snapshot.external_ref)
            if espo_id is None:
                resp = self._client.post("/Lead", json=payload)
            else:
                resp = self._client.put(f"/Lead/{espo_id}", json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise CRMError(f"espo upsert failed: {exc}") from exc
        espo_id = resp.json()["id"]
        log.info("espo lead upserted", espo_id=espo_id, state=snapshot.state)
        return espo_id

    def delete_lead(self, external_ref: str) -> bool:
        espo_id = self._find(external_ref)
        if espo_id is None:
            return False
        try:
            resp = self._client.delete(f"/Lead/{espo_id}")
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise CRMError(f"espo delete failed: {exc}") from exc
        log.info("espo lead deleted", espo_id=espo_id)
        return True

    def record_event(self, external_ref: str, kind: str, detail: str) -> None:
        espo_id = self._find(external_ref)
        if espo_id is None:
            raise CRMError("espo lead not found for event")
        try:
            resp = self._client.post(
                "/Note",
                json={
                    "type": "Post",
                    "parentType": "Lead",
                    "parentId": espo_id,
                    "post": f"[{kind}] {detail}",
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise CRMError(f"espo note failed: {exc}") from exc
