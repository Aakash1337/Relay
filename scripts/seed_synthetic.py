"""Phase 1A demo: seed a synthetic campaign and run the whole cohort.

Seeds N Faker prospects (edge cases included: injection bios, unicode
names, sparse records), runs every lead to the human gate, approves each
pending draft through the rubric, lets the worker execute the simulated
sends, triages the hash-stable replies, and prints the funnel + the
economics report at the end.

    just seed          (or: uv run python scripts/seed_synthetic.py [n])
"""

from __future__ import annotations

import sys
import uuid
from collections import Counter

from sqlalchemy import select

from relay.db.engine import admin_session, tenant_session
from relay.db.models import Lead, OutreachDraft, Tenant
from relay.domain.approval import review_draft
from relay.domain.vocab import ReviewDecision
from relay.economics import campaign_economics
from relay.hashing import hash_api_key
from relay.logs import setup_logging
from relay.pipeline.runner import PipelineRunner
from relay.synthetic.seed import seed_campaign
from relay.workers.send_worker import process_pending


def main() -> None:
    setup_logging()
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    suffix = uuid.uuid4().hex[:8]

    with admin_session() as session:
        tenant = Tenant(
            name=f"seed-tenant-{suffix}",
            api_key_hash=hash_api_key(f"seed-key-{suffix}"),
        )
        session.add(tenant)
        session.flush()
        tenant_id = tenant.id

    result = seed_campaign(tenant_id, n=n, campaign_name=f"seed-{suffix}")
    print(
        f"\n→ seeded {len(result.lead_ids)} prospects "
        f"({result.skipped_duplicates} duplicates rejected by the dedup "
        "constraint)"
    )

    # Run each lead to the human gate (or an earlier rejection).
    for lead_id in result.lead_ids:
        PipelineRunner(tenant_id, lead_id=lead_id).run()

    # The human gate, en masse: approve every pending draft via the rubric.
    with tenant_session(tenant_id) as session:
        pending = (
            session.execute(
                select(OutreachDraft).where(OutreachDraft.status == "pending_approval")
            )
            .scalars()
            .all()
        )
        for draft in pending:
            review_draft(
                session,
                draft=draft,
                reviewer="seed-operator",
                decision=ReviewDecision.APPROVED,
            )
    print(f"→ human gate: approved {len(pending)} drafts (nothing sent)")

    for lead_id in result.lead_ids:
        PipelineRunner(tenant_id, lead_id=lead_id).run()
    stats = process_pending()
    print(f"→ worker: {stats.sent} simulated sends")

    # Replies + triage (hash-stable personas: some decline, some opt out).
    for lead_id in result.lead_ids:
        PipelineRunner(tenant_id, lead_id=lead_id).run()

    with tenant_session(tenant_id) as session:
        states = Counter(session.execute(select(Lead.state)).scalars().all())
    print("\nCohort outcome:")
    for state, count in states.most_common():
        print(f"  {state:25s} {count}")

    report = campaign_economics(tenant_id, result.campaign_id)
    print("\nEconomics (guardrail units):")
    for stage, count in report.funnel.items():
        print(f"  {stage:12s} {count}")
    print(f"  cost total   {report.cost_units_total:.2f} units")
    if report.cost_units_per_meeting is not None:
        print(f"  per meeting  {report.cost_units_per_meeting:.2f} units")
    else:
        print("  per meeting  n/a (no bookings in this cohort)")


if __name__ == "__main__":
    main()
