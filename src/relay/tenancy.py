"""Tenant-isolation primitives (project documentation §3).

Full multi-tenancy productization is Phase 4; these primitives exist from
Phase 0 because retrofitting them is a rewrite:

- every table carries ``tenant_id`` and Postgres enforces row-level
  security (see src/relay/db/sql/004_rls.sql);
- vector-store namespaces are tenant-scoped (helper here, store in a later
  phase);
- encryption keys are derived per tenant (helper in relay.hashing);
- credentials, suppression, idempotency, and logging are tenant-scoped by
  schema design.
"""

from __future__ import annotations

import uuid

from relay.config import get_settings
from relay.hashing import derive_tenant_key


def vector_namespace(tenant_id: uuid.UUID | str) -> str:
    """Namespace under which a tenant's vectors live — never shared."""
    return f"tenant_{tenant_id}"


def tenant_encryption_key(tenant_id: uuid.UUID | str, purpose: str) -> bytes:
    """Per-tenant, per-purpose key (e.g. 'oauth_tokens', 'raw_email')."""
    master = get_settings().master_key.get_secret_value()
    return derive_tenant_key(master, str(tenant_id), purpose)
