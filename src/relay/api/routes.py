"""HTTP routes — the validated boundary in front of RELAY's actions (§14).

Note what is NOT here: a send endpoint. Approval moves a draft to
``approved`` and nothing more; execution belongs to the internal worker.
The only worker surface is an admin-token-protected tick used by the
n8n spine's schedule — it processes the queue through the full
eligibility re-check, it cannot "just send" anything.
"""

from __future__ import annotations

import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from relay import audit
from relay.api import schemas
from relay.api.deps import require_admin, require_tenant
from relay.db.engine import admin_session, tenant_session
from relay.db.models import (
    Campaign,
    Lead,
    LeadSourceRegister,
    LeadTransition,
    OutreachDraft,
    SendJob,
    Tenant,
)
from relay.domain.approval import ApprovalError, approve_draft, reject_draft
from relay.guardrails.harness import GuardrailViolation
from relay.hashing import email_domain, hash_api_key, hash_email
from relay.logs import get_logger
from relay.pipeline.runner import PipelineRunner
from relay.workers.send_worker import process_pending

log = get_logger(__name__)

router = APIRouter()

# ── Tenant bootstrap (admin) ───────────────────────────────────────────────


@router.post(
    "/tenants",
    response_model=schemas.TenantCreateResponse,
    dependencies=[Depends(require_admin)],
    status_code=status.HTTP_201_CREATED,
)
def create_tenant(
    body: schemas.TenantCreateRequest,
) -> schemas.TenantCreateResponse:
    api_key = f"rk_{secrets.token_urlsafe(32)}"
    try:
        with admin_session() as session:
            tenant = Tenant(name=body.name, api_key_hash=hash_api_key(api_key))
            session.add(tenant)
            session.flush()
            tenant_id = tenant.id
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="tenant name already exists"
        ) from exc
    return schemas.TenantCreateResponse(id=tenant_id, name=body.name, api_key=api_key)


# ── Lead source register ───────────────────────────────────────────────────


@router.post(
    "/sources",
    response_model=schemas.SourceResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_source(
    body: schemas.SourceCreateRequest,
    tenant_id: uuid.UUID = Depends(require_tenant),
) -> schemas.SourceResponse:
    try:
        with tenant_session(tenant_id) as session:
            source = LeadSourceRegister(tenant_id=tenant_id, **body.model_dump())
            session.add(source)
            session.flush()
            response = schemas.SourceResponse(
                id=source.id,
                name=source.name,
                source_type=source.source_type,
                terms_allow_use=source.terms_allow_use,
            )
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="source name already exists"
        ) from exc
    return response


# ── Campaigns ───────────────────────────────────────────────────────────────


@router.post(
    "/campaigns",
    response_model=schemas.CampaignResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_campaign(
    body: schemas.CampaignCreateRequest,
    tenant_id: uuid.UUID = Depends(require_tenant),
) -> schemas.CampaignResponse:
    try:
        with tenant_session(tenant_id) as session:
            campaign = Campaign(tenant_id=tenant_id, **body.model_dump())
            session.add(campaign)
            session.flush()
            response = schemas.CampaignResponse(
                id=campaign.id,
                name=campaign.name,
                dry_run=campaign.dry_run,
                simulated_replies_enabled=campaign.simulated_replies_enabled,
                status=campaign.status,
            )
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="campaign name already exists"
        ) from exc
    return response


@router.get(
    "/campaigns/{campaign_id}/status",
    response_model=schemas.CampaignStatusResponse,
)
def campaign_status(
    campaign_id: uuid.UUID,
    tenant_id: uuid.UUID = Depends(require_tenant),
) -> schemas.CampaignStatusResponse:
    with tenant_session(tenant_id) as session:
        campaign = session.get(Campaign, campaign_id)
        if campaign is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "campaign not found")
        lead_states = dict(
            session.execute(
                select(Lead.state, func.count())
                .where(Lead.campaign_id == campaign_id)
                .group_by(Lead.state)
            ).all()
        )
        job_states = dict(
            session.execute(
                select(SendJob.status, func.count())
                .where(SendJob.campaign_id == campaign_id)
                .group_by(SendJob.status)
            ).all()
        )
        return schemas.CampaignStatusResponse(
            campaign_id=campaign_id,
            name=campaign.name,
            dry_run=campaign.dry_run,
            lead_states=lead_states,
            send_jobs=job_states,
        )


# ── Leads ───────────────────────────────────────────────────────────────────


def _lead_response(lead: Lead) -> schemas.LeadResponse:
    return schemas.LeadResponse(
        id=lead.id,
        campaign_id=lead.campaign_id,
        state=lead.state,
        dry_run=lead.dry_run,
        email_verified=lead.email_verified,
        lawful_basis=lead.lawful_basis,
        region_assumption=lead.region_assumption,
        fit_score=float(lead.fit_score) if lead.fit_score is not None else None,
        approved_message_version=lead.approved_message_version,
    )


@router.post(
    "/leads",
    response_model=schemas.LeadResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_lead(
    body: schemas.LeadCreateRequest,
    tenant_id: uuid.UUID = Depends(require_tenant),
) -> schemas.LeadResponse:
    try:
        with tenant_session(tenant_id) as session:
            source = session.get(LeadSourceRegister, body.source_id)
            if source is None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "source_id not found in lead source register",
                )
            lead = Lead(
                tenant_id=tenant_id,
                campaign_id=body.campaign_id,
                source_id=body.source_id,
                # Snapshot of the register's answer at ingestion time; the
                # DB CHECK requires 'yes' or the insert fails.
                source_terms_status=source.terms_allow_use,
                lawful_basis=body.lawful_basis,
                region_assumption=body.region_assumption,
                email=str(body.email),
                email_hash=hash_email(str(body.email)),
                email_domain=email_domain(str(body.email)),
                dry_run=body.dry_run,
                first_name=body.first_name,
                last_name=body.last_name,
                title=body.title,
                company_name=body.company_name,
                company_domain=body.company_domain,
            )
            session.add(lead)
            audit.record(
                session,
                tenant_id=tenant_id,
                actor_type="system",
                actor_id="api:create_lead",
                action="lead.create",
                entity_type="lead",
                payload={
                    "source_id": str(body.source_id),
                    "lawful_basis": body.lawful_basis,
                    "region_assumption": body.region_assumption,
                    "email": str(body.email),  # redacted to a hash in audit
                },
            )
            session.flush()
            response = _lead_response(lead)
    except IntegrityError as exc:
        message = str(exc.orig)
        if "uq_leads_campaign_email" in message:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "duplicate lead: this address already exists in the campaign",
            ) from exc
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"lead violates a structural constraint: {message}",
        ) from exc
    return response


@router.get("/leads/{lead_id}", response_model=schemas.LeadResponse)
def get_lead(
    lead_id: uuid.UUID,
    tenant_id: uuid.UUID = Depends(require_tenant),
) -> schemas.LeadResponse:
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "lead not found")
        return _lead_response(lead)


@router.get("/leads/{lead_id}/trace", response_model=schemas.LeadTraceResponse)
def lead_trace(
    lead_id: uuid.UUID,
    tenant_id: uuid.UUID = Depends(require_tenant),
) -> schemas.LeadTraceResponse:
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        if lead is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "lead not found")
        transitions = (
            session.execute(
                select(LeadTransition)
                .where(LeadTransition.lead_id == lead_id)
                .order_by(LeadTransition.created_at, LeadTransition.id)
            )
            .scalars()
            .all()
        )
        return schemas.LeadTraceResponse(
            lead_id=lead_id,
            state=lead.state,
            transitions=[
                schemas.TraceEntry(
                    from_state=t.from_state,
                    to_state=t.to_state,
                    actor=t.actor,
                    reason=t.reason,
                    run_id=t.run_id,
                    created_at=t.created_at,
                )
                for t in transitions
            ],
        )


# ── Pipeline runs ───────────────────────────────────────────────────────────


@router.post("/leads/{lead_id}/pipeline/run", response_model=schemas.RunResponse)
def run_pipeline(
    lead_id: uuid.UUID,
    body: schemas.RunRequest,
    tenant_id: uuid.UUID = Depends(require_tenant),
) -> schemas.RunResponse:
    with tenant_session(tenant_id) as session:
        if session.get(Lead, lead_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "lead not found")
    runner = PipelineRunner(
        tenant_id,
        lead_id=lead_id,
        max_iterations=body.max_iterations,
        budget_units=body.budget_units,
    )
    try:
        outcome = runner.run()
    except GuardrailViolation as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"run killed by guardrail: {exc}"
        ) from exc
    return schemas.RunResponse(
        run_id=outcome.run_id,
        lead_id=outcome.lead_id,
        final_state=outcome.final_state,
        steps=outcome.steps,
        cost_units=outcome.cost_units,
        stopped_on=outcome.stopped_on,
        visited=outcome.visited,
    )


# ── Human gate (§10: approve ≠ send) ────────────────────────────────────────


@router.post(
    "/outreach-drafts/{draft_id}/approve",
    response_model=schemas.ApproveResponse,
)
def approve(
    draft_id: uuid.UUID,
    body: schemas.ApproveRequest,
    tenant_id: uuid.UUID = Depends(require_tenant),
) -> schemas.ApproveResponse:
    with tenant_session(tenant_id) as session:
        draft = session.get(OutreachDraft, draft_id)
        if draft is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "draft not found")
        try:
            approve_draft(session, draft=draft, approver=body.approver)
        except ApprovalError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        lead = session.get(Lead, draft.lead_id)
        assert lead is not None
        return schemas.ApproveResponse(
            draft_id=draft.id,
            version=draft.version,
            approved=True,
            lead_state=lead.state,
        )


@router.post("/outreach-drafts/{draft_id}/reject")
def reject(
    draft_id: uuid.UUID,
    body: schemas.RejectRequest,
    tenant_id: uuid.UUID = Depends(require_tenant),
) -> dict[str, str]:
    with tenant_session(tenant_id) as session:
        draft = session.get(OutreachDraft, draft_id)
        if draft is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "draft not found")
        try:
            reject_draft(
                session, draft=draft, approver=body.approver, reason=body.reason
            )
        except ApprovalError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return {"status": "rejected"}


# ── Internal: spine-triggered worker tick (admin token, not tenant key) ─────


@router.post(
    "/internal/send-worker/tick",
    response_model=schemas.WorkerTickResponse,
    dependencies=[Depends(require_admin)],
)
def send_worker_tick() -> schemas.WorkerTickResponse:
    stats = process_pending()
    return schemas.WorkerTickResponse(
        sent=stats.sent, blocked=stats.blocked, failed=stats.failed
    )
