"""API dependencies: tenant resolution and admin protection.

The tenant is derived from the API key and nothing else — request bodies
never choose a tenant. Every handler then operates inside a
tenant-pinned session, and Postgres RLS enforces the boundary.
"""

from __future__ import annotations

import secrets
import uuid

from fastapi import Header, HTTPException, status
from sqlalchemy import text

from relay.config import get_settings
from relay.db.engine import untenanted_app_session
from relay.hashing import hash_api_key


def require_tenant(
    x_api_key: str = Header(description="Tenant API key"),
) -> uuid.UUID:
    key_hash = hash_api_key(x_api_key)
    with untenanted_app_session() as session:
        tenant_id = session.execute(
            text("SELECT fn_tenant_id_for_api_key(:h)"), {"h": key_hash}
        ).scalar()
    if tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid API key",
        )
    return tenant_id


def require_admin(
    x_admin_token: str = Header(description="Admin bootstrap token"),
) -> None:
    configured = get_settings().admin_token
    if configured is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin endpoints are disabled (no RELAY_ADMIN_TOKEN set)",
        )
    if not secrets.compare_digest(x_admin_token, configured.get_secret_value()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid admin token",
        )
