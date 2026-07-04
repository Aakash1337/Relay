"""CRM sync seam (Phase 1A) — one adapter interface, config-selected target.

RELAY's datastore stays canonical; the CRM is a downstream mirror for
humans. Sync is one-way (RELAY → CRM), best-effort, and never on the
send path: a CRM outage cannot block or delay safety gates, and a CRM
row is never consulted for any decision.
"""

from relay.crm.base import CRMAdapter, CRMConfigError, CRMError, CRMLeadSnapshot
from relay.crm.registry import crm_adapter, reset_crm
from relay.crm.sync import sync_lead

__all__ = [
    "CRMAdapter",
    "CRMConfigError",
    "CRMError",
    "CRMLeadSnapshot",
    "crm_adapter",
    "reset_crm",
    "sync_lead",
]
