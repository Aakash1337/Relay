"""Walk one synthetic lead through the entire pipeline and print the trace.

This is the Phase 0 exit-gate demo: an empty pipeline (no real work, no
real PII, no real sending — a synthetic prospect at an example.test
address) moving through every state with the guardrail harness counting
every step, the human gate exercised explicitly, and the internal worker
executing a *simulated* send.

    just demo
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from relay.db.engine import admin_session, tenant_session
from relay.db.models import (
    Campaign,
    Lead,
    LeadSourceRegister,
    LeadTransition,
    OutreachDraft,
    PipelineRun,
    Tenant,
)
from relay.domain.approval import approve_draft
from relay.hashing import email_domain, hash_api_key, hash_email
from relay.logs import setup_logging
from relay.pipeline.runner import PipelineRunner
from relay.workers.send_worker import process_pending


def main() -> None:
    setup_logging()
    suffix = uuid.uuid4().hex[:8]

    # 1. Bootstrap a demo tenant (admin path, as the API would).
    with admin_session() as session:
        tenant = Tenant(
            name=f"demo-tenant-{suffix}",
            api_key_hash=hash_api_key(f"demo-key-{suffix}"),
        )
        session.add(tenant)
        session.flush()
        tenant_id = tenant.id

    # 2. Register a synthetic source, a dry-run campaign, one fake lead.
    email = f"prospect-{suffix}@example.test"
    with tenant_session(tenant_id) as session:
        source = LeadSourceRegister(
            tenant_id=tenant_id,
            name="synthetic-demo",
            source_type="synthetic",
            terms_allow_use="yes",
            personal_data_collected=[],
            proof_of_lawful_use="synthetic data — no real person",
        )
        campaign = Campaign(
            tenant_id=tenant_id,
            name=f"demo-campaign-{suffix}",
            dry_run=True,
            simulated_replies_enabled=True,  # explicit seed/test mode
        )
        session.add_all([source, campaign])
        session.flush()
        lead = Lead(
            tenant_id=tenant_id,
            campaign_id=campaign.id,
            source_id=source.id,
            source_terms_status="yes",
            lawful_basis="synthetic",
            region_assumption="none-synthetic",
            email=email,
            email_hash=hash_email(email),
            email_domain=email_domain(email),
            dry_run=True,
        )
        session.add(lead)
        session.flush()
        lead_id = lead.id

    # 3. Run the pipeline to the human gate.
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    print(f"\n→ paused at {outcome.final_state} ({outcome.stopped_on})")

    # 4. Human gate: approve the draft. This does NOT send.
    with tenant_session(tenant_id) as session:
        draft = session.execute(
            select(OutreachDraft).where(OutreachDraft.lead_id == lead_id)
        ).scalar_one()
        approve_draft(session, draft=draft, approver="demo-operator")
    print("→ draft approved by human gate (nothing sent)")

    # 5. Continue: eligibility gate → send_queued.
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    print(f"→ paused at {outcome.final_state} ({outcome.stopped_on})")

    # 6. Internal worker executes the SIMULATED send.
    stats = process_pending()
    print(
        f"→ worker: sent={stats.sent} blocked={stats.blocked} "
        f"failed={stats.failed} (mode: simulated — nothing left the machine)"
    )

    # 7. Continue to the end: simulated reply → triage → booked → closed.
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    print(f"→ finished at {outcome.final_state} ({outcome.stopped_on})")

    # 8. Print the full journey from the database trace.
    with tenant_session(tenant_id) as session:
        rows = (
            session.execute(
                select(LeadTransition)
                .where(LeadTransition.lead_id == lead_id)
                .order_by(LeadTransition.created_at, LeadTransition.id)
            )
            .scalars()
            .all()
        )
        runs = (
            session.execute(select(PipelineRun).order_by(PipelineRun.started_at))
            .scalars()
            .all()
        )
        print(f"\nLead journey ({len(rows)} transitions):")
        for row in rows:
            arrow = f"{row.from_state} → {row.to_state}"
            print(f"  {arrow:55s} [{row.actor}]")
        print("\nGuardrailed runs:")
        for run in runs:
            print(
                f"  {run.kind}: {run.status} — {run.iterations} iterations, "
                f"{run.cost_units} cost units "
                f"(caps: {run.max_iterations} it / {run.budget_units} units)"
            )


if __name__ == "__main__":
    main()
