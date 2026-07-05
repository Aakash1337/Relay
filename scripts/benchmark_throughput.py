"""Throughput benchmark (Phase 4 exit-gate instrument).

Seeds N tenants x M leads (synthetic, dry-run — nothing can leave), runs
the full funnel with concurrent pipelines, batch-approves every draft,
and drains the queue with the scaled worker. Prints per-phase wall-clock
and leads/second so an operator can test a real throughput target on
real hardware:

    uv run python scripts/benchmark_throughput.py --tenants 2 --leads 10 \\
        --concurrency 4

Uses whatever compute backends are configured; with the hermetic
'offline' default the numbers measure RELAY itself (datastore, gates,
worker), not a model provider.
"""

from __future__ import annotations

import argparse
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from relay.db.engine import tenant_session
from relay.db.models import OutreachDraft
from relay.domain.approval import review_draft
from relay.logs import setup_logging
from relay.pipeline.runner import PipelineRunner
from relay.workers.send_worker import process_pending


def _provision(tenants: int, leads: int) -> dict[uuid.UUID, list[uuid.UUID]]:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tests.conftest import LeadFactory, _create_tenant

    cohorts: dict[uuid.UUID, list[uuid.UUID]] = {}
    for i in range(tenants):
        tenant_id, _ = _create_tenant(f"bench-{uuid.uuid4().hex[:8]}-{i}")
        factory = LeadFactory(tenant_id)
        campaign_id = factory.campaign(simulated_replies=False)
        cohorts[tenant_id] = [
            factory.lead(campaign_id=campaign_id) for _ in range(leads)
        ]
    return cohorts


def _approve_all(tenant_id: uuid.UUID) -> int:
    approved = 0
    with tenant_session(tenant_id) as session:
        drafts = (
            session.execute(
                select(OutreachDraft).where(OutreachDraft.status == "pending_approval")
            )
            .scalars()
            .all()
        )
        for draft in drafts:
            review_draft(session, draft=draft, reviewer="bench", decision="approved")
            approved += 1
    return approved


def run_benchmark(tenants: int, leads: int, concurrency: int) -> dict[str, float]:
    total = tenants * leads
    cohorts = _provision(tenants, leads)
    pairs = [(t, x) for t, cohort in cohorts.items() for x in cohort]

    timings: dict[str, float] = {}

    def timed(name: str, fn) -> None:  # noqa: ANN001
        start = time.perf_counter()
        fn()
        timings[name] = time.perf_counter() - start

    def to_gate() -> None:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(lambda p: PipelineRunner(p[0], lead_id=p[1]).run(), pairs))

    def approve() -> None:
        for tenant_id in cohorts:
            _approve_all(tenant_id)

    def to_queue() -> None:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(lambda p: PipelineRunner(p[0], lead_id=p[1]).run(), pairs))

    def drain() -> None:
        stats = process_pending(max_jobs=total * 2, concurrency=concurrency)
        if stats.sent != total:
            raise RuntimeError(
                f"benchmark drain sent {stats.sent} of {total} "
                f"(blocked={stats.blocked} failed={stats.failed})"
            )

    timed("pipeline_to_gate", to_gate)
    timed("approve_all", approve)
    timed("queue_eligibility", to_queue)
    timed("worker_drain", drain)
    timings["total"] = sum(timings.values())

    print(
        f"\nRELAY throughput — {tenants} tenant(s) x {leads} lead(s), "
        f"concurrency {concurrency}"
    )
    print(f"{'phase':<20}{'seconds':>10}{'leads/sec':>12}")
    for phase, seconds in timings.items():
        rate = total / seconds if seconds else float("inf")
        print(f"{phase:<20}{seconds:>10.2f}{rate:>12.1f}")
    return timings


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="RELAY throughput benchmark")
    parser.add_argument("--tenants", type=int, default=2)
    parser.add_argument("--leads", type=int, default=10, help="per tenant")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    run_benchmark(args.tenants, args.leads, args.concurrency)


if __name__ == "__main__":
    main()
