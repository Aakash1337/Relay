"""Retention purge worker (Phase 1B) — internal-only, like the send worker.

Finds tenants holding leads past ``retention_until`` and erases those
leads through the same audited DSR path (``fn_dsr_erase``), one tenant
context at a time. No suppression entries are written: a lapsed lawful
basis is not an opt-out, and fabricating one would corrupt the
suppression list's meaning.

    uv run relay-retention          # one pass over all tenants
"""

from __future__ import annotations

import argparse
import uuid
from dataclasses import dataclass, field

from sqlalchemy import text

from relay.db.engine import untenanted_app_session
from relay.domain.dsr import purge_expired
from relay.logs import get_logger, setup_logging

log = get_logger(__name__)


@dataclass
class PurgeStats:
    tenants: int = 0
    leads_purged: int = 0
    per_tenant: dict[str, int] = field(default_factory=dict)


def _tenants_with_expired_leads() -> list[uuid.UUID]:
    with untenanted_app_session() as session:
        rows = session.execute(text("SELECT fn_tenants_with_expired_leads()")).scalars()
        return list(rows)


def run_once() -> PurgeStats:
    stats = PurgeStats()
    for tenant_id in _tenants_with_expired_leads():
        purged = purge_expired(tenant_id)
        stats.tenants += 1
        stats.leads_purged += purged
        stats.per_tenant[str(tenant_id)] = purged
    log.info(
        "retention pass complete",
        tenants=stats.tenants,
        leads_purged=stats.leads_purged,
    )
    return stats


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="RELAY retention purge worker")
    parser.add_argument(
        "--once",
        action="store_true",
        default=True,
        help="run one purge pass (the only mode; scheduling is the spine's job)",
    )
    parser.parse_args()
    run_once()


if __name__ == "__main__":
    main()
