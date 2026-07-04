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


@contextmanager
def tenant_session(tenant_id: uuid.UUID | str) -> Iterator[Session]:
    """A session pinned to one tenant for its entire lifetime.

    ``app.tenant_id`` is (re)applied at the start of every transaction the
    session opens, using ``set_config(..., is_local => true)`` so the
    setting can never leak across transactions on a pooled connection.
    """
    session = Session(app_engine())

    @event.listens_for(session, "after_begin")
    def _pin_tenant(  # noqa: ANN001 — SQLAlchemy event signature
        _session, _transaction, connection
    ) -> None:
        connection.exec_driver_sql(
            "SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),)
        )

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
