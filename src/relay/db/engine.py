"""Engines and sessions.

Two engines, two trust levels:

- **admin engine** — migrations and tenant bootstrap only. Connects as the
  schema owner and is never handed to request/pipeline code.
- **app engine** — everything else. Connects as ``relay_app``, a
  non-superuser subject to FORCEd row-level security. Every transaction
  pins ``app.tenant_id``; without it, every tenant-scoped query returns
  nothing and every write is rejected.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session

from relay.config import get_settings


@lru_cache(maxsize=1)
def admin_engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True)


@lru_cache(maxsize=1)
def app_engine() -> Engine:
    return create_engine(get_settings().app_database_url, pool_pre_ping=True)


def reset_engines() -> None:
    """Dispose cached engines (tests / config reload)."""
    for cached in (admin_engine, app_engine):
        with contextlib.suppress(Exception):  # best-effort disposal
            cached().dispose()
        cached.cache_clear()


@event.listens_for(Session, "after_begin")
def _pin_tenant_from_info(  # noqa: ANN001 — SQLAlchemy event signature
    session, _transaction, connection
) -> None:
    """Re-pin app.tenant_id at the start of every transaction.

    One class-level listener (registered once at import) reads the tenant
    from ``session.info`` rather than a per-session closure — otherwise
    every tenant_session() would register a new listener that lives in the
    global event registry until GC. ``is_local => true`` scopes the setting
    to the transaction so it never leaks across a pooled connection.
    """
    tenant_id = session.info.get("relay_tenant_id")
    if tenant_id is not None:
        connection.exec_driver_sql(
            "SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),)
        )


@contextmanager
def tenant_session(tenant_id: uuid.UUID | str) -> Iterator[Session]:
    """A session pinned to one tenant for its entire lifetime.

    ``app.tenant_id`` is (re)applied at the start of every transaction the
    session opens (see the class-level listener above).
    """
    session = Session(app_engine(), info={"relay_tenant_id": str(tenant_id)})
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def untenanted_app_session() -> Iterator[Session]:
    """App-role session with NO tenant context.

    Only for calls that are safe without one (SECURITY DEFINER lookups
    such as API-key → tenant resolution and the worker's tenant listing).
    All tenant-scoped tables read as empty here — that is the point.
    """
    session = Session(app_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def admin_session() -> Iterator[Session]:
    """Schema-owner session. Migrations and bootstrap only."""
    session = Session(admin_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
