"""Seed a tenant with synthetic prospects and simulate replies.

The write path is the real one — the source register, provenance
fields, dedup constraint, and lead-insert guard all apply. Seeding does
not get a side door into the datastore; it walks in the front like any
other source (that is half the point of seeding).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from relay.db.engine import tenant_session
from relay.db.models import Campaign, Lead, LeadSourceRegister, Reply, SendJob
from relay.hashing import email_domain, hash_email
from relay.logs import get_logger
from relay.synthetic.generator import (
    ReplyIntent,
    generate_prospects,
    simulated_reply_text,
)

log = get_logger(__name__)

_INTENTS = list(ReplyIntent)


@dataclass(frozen=True)
class SeedResult:
    source_id: uuid.UUID
    campaign_id: uuid.UUID
    lead_ids: list[uuid.UUID]
    skipped_duplicates: int


def seed_campaign(
    tenant_id: uuid.UUID,
    *,
    n: int = 20,
    seed: int = 1337,
    campaign_name: str | None = None,
) -> SeedResult:
    """Create a synthetic source + dry-run campaign + ``n`` leads.

    Duplicate emails (possible at small domain pools) are skipped and
    counted — the dedup constraint is doing its job, not failing.
    """
    prospects = generate_prospects(n, seed=seed)

    with tenant_session(tenant_id) as session:
        source = LeadSourceRegister(
            tenant_id=tenant_id,
            name=f"synthetic-seed-{seed}",
            source_type="synthetic",
            terms_allow_use="yes",
            proof_of_lawful_use="Faker-generated; no real persons (Phase 1A)",
        )
        campaign = Campaign(
            tenant_id=tenant_id,
            name=campaign_name or f"synthetic-campaign-{seed}",
            dry_run=True,
            simulated_replies_enabled=True,
        )
        session.add_all([source, campaign])
        session.flush()
        source_id, campaign_id = source.id, campaign.id

    lead_ids: list[uuid.UUID] = []
    skipped = 0
    for prospect in prospects:
        try:
            with tenant_session(tenant_id) as session:
                lead = Lead(
                    tenant_id=tenant_id,
                    campaign_id=campaign_id,
                    source_id=source_id,
                    source_terms_status="yes",
                    lawful_basis="synthetic",
                    region_assumption="none-synthetic",
                    email=prospect.email,
                    email_hash=hash_email(prospect.email),
                    email_domain=email_domain(prospect.email),
                    first_name=prospect.first_name,
                    last_name=prospect.last_name,
                    title=prospect.title,
                    company_name=prospect.company or None,
                    company_domain=prospect.company_domain,
                    bio=prospect.bio,
                    dry_run=True,
                )
                session.add(lead)
                session.flush()
                lead_ids.append(lead.id)
        except IntegrityError:
            skipped += 1

    log.info(
        "seeded synthetic campaign",
        campaign_id=str(campaign_id),
        leads=len(lead_ids),
        skipped_duplicates=skipped,
    )
    return SeedResult(
        source_id=source_id,
        campaign_id=campaign_id,
        lead_ids=lead_ids,
        skipped_duplicates=skipped,
    )


def intent_for_lead(lead: Lead) -> ReplyIntent:
    """Deterministic reply intent per lead (hash-derived, like the offline
    backend's scores) — reseeding or re-running never changes a persona."""
    digest = int(lead.email_hash[:8], 16)
    return _INTENTS[digest % len(_INTENTS)]


def create_simulated_reply(
    tenant_id: uuid.UUID,
    lead_id: uuid.UUID,
    *,
    intent: ReplyIntent | None = None,
) -> uuid.UUID:
    """Insert a simulated inbound reply for a lead whose send completed.

    Refuses unless the campaign opted into simulated replies AND the send
    job exists and is 'sent' — a reply to nothing is a lie in the data.
    """
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            raise ValueError("lead not found in tenant scope")
        campaign = session.get(Campaign, lead.campaign_id)
        assert campaign is not None
        if not campaign.simulated_replies_enabled:
            raise ValueError("campaign has simulated replies disabled")
        job = session.execute(
            select(SendJob).where(SendJob.lead_id == lead_id, SendJob.status == "sent")
        ).scalar_one_or_none()
        if job is None:
            raise ValueError("no completed send to reply to")

        chosen = intent or intent_for_lead(lead)
        reply = Reply(
            tenant_id=tenant_id,
            lead_id=lead_id,
            campaign_id=lead.campaign_id,
            send_job_id=job.id,
            simulated=True,
            subject="Re: your note",
            body=simulated_reply_text(chosen, variant=int(lead.email_hash[8], 16)),
        )
        session.add(reply)
        session.flush()
        log.info(
            "simulated reply created",
            lead_id=str(lead_id),
            intent=str(chosen),
        )
        return reply.id
