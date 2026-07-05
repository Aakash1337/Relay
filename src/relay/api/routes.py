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

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from relay import audit
from relay.api import schemas
from relay.api.deps import require_admin, require_tenant
from relay.config import get_settings
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
from relay.domain import dsr, preflight
from relay.domain.approval import (
    ApprovalError,
    approve_draft,
    reject_draft,
    review_draft,
)
from relay.domain.state_machine import TransitionError
from relay.domain.suppression import add_suppression
from relay.economics import campaign_economics, tenant_economics
from relay.guardrails.harness import GuardrailViolation
from relay.hashing import email_domain, hash_api_key, hash_email
from relay.ingest.ses_events import EventRejected, process_sns_envelope
from relay.ingest.unsubscribe import (
    UnsubscribeRejected,
    process_unsubscribe,
    verify_token,
)
from relay.logs import get_logger
from relay.observability import evaluate_alerts, prometheus_text, tenant_metrics
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


@router.post(
    "/internal/tenants/{tenant_id}/rotate-key",
    response_model=schemas.TenantKeyRotateResponse,
    dependencies=[Depends(require_admin)],
)
def rotate_tenant_key(tenant_id: uuid.UUID) -> schemas.TenantKeyRotateResponse:
    """Rotate a tenant's API key (Phase 3: secrets rotation).

    The old key stops working the moment this commits — rotation is for
    suspected exposure, so a grace overlap would defeat the point. The
    new key is returned exactly once; only its hash is stored.
    """
    api_key = f"rk_{secrets.token_urlsafe(32)}"
    with admin_session() as session:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant not found")
        tenant.api_key_hash = hash_api_key(api_key)
        audit.record(
            session,
            tenant_id=tenant_id,
            actor_type="human",
            actor_id="admin",
            action="tenant.rotate_key",
            entity_type="tenant",
            entity_id=str(tenant_id),
            payload={"note": "api key rotated; old key invalidated"},
        )
    return schemas.TenantKeyRotateResponse(id=tenant_id, api_key=api_key)


@router.post(
    "/internal/tenants/{tenant_id}/attest-sender-identity",
    response_model=schemas.TenantSenderAttestResponse,
    dependencies=[Depends(require_admin)],
)
def attest_sender_identity(
    tenant_id: uuid.UUID,
) -> schemas.TenantSenderAttestResponse:
    """Record the operator attest that this tenant's sender_from_address
    is provider-verified (Phase 4). Until attested, real sends for a
    tenant with its own address are blocked at eligibility — before the
    provider can reject the unverified From into terminal failures."""
    with admin_session() as session:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant not found")
        if tenant.sender_from_address is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "tenant has no sender_from_address to attest",
            )
        tenant.sender_identity_verified = True
        audit.record(
            session,
            tenant_id=tenant_id,
            actor_type="human",
            actor_id="admin",
            action="tenant.attest_sender_identity",
            entity_type="tenant",
            entity_id=str(tenant_id),
            payload={"verified": True},
        )
        return schemas.TenantSenderAttestResponse(
            id=tenant_id,
            sender_from_address=tenant.sender_from_address,
            sender_identity_verified=True,
        )


@router.post(
    "/internal/suppression/global",
    response_model=schemas.GlobalSuppressionResponse,
    dependencies=[Depends(require_admin)],
    status_code=status.HTTP_201_CREATED,
)
def add_global_suppression(
    body: schemas.GlobalSuppressionRequest,
) -> schemas.GlobalSuppressionResponse:
    """Create a platform-wide do-not-contact entry (§17 scope decision).

    Global scope blocks EVERY tenant's sends to the address, so it is an
    admin action: RLS rejects scope='global' from the application role.
    Runs on the admin connection (definer_bypass policy).
    """
    from relay.db.engine import admin_session as _admin_session

    with _admin_session() as session:
        if session.get(Tenant, body.tenant_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "tenant not found")
        entry = add_suppression(
            session,
            tenant_id=body.tenant_id,
            reason=body.reason,
            source="manual",
            created_by="admin",
            actor_type="human",
            email=str(body.email),
            scope="global",
        )
        return schemas.GlobalSuppressionResponse(
            id=entry.id, email_hash=entry.email_hash, reason=entry.reason
        )


@router.post(
    "/internal/tenants/onboard",
    response_model=schemas.TenantOnboardResponse,
    dependencies=[Depends(require_admin)],
    status_code=status.HTTP_201_CREATED,
)
def onboard_tenant(
    body: schemas.TenantOnboardRequest,
) -> schemas.TenantOnboardResponse:
    """Self-serve onboarding (Phase 4): tenant + key + source + campaign
    + quotas in ONE atomic call — a new client starts working without
    anyone hand-editing config. Everything else (leads, pipeline, review)
    happens through the tenant's own API key."""
    api_key = f"rk_{secrets.token_urlsafe(32)}"
    try:
        with admin_session() as session:
            tenant = Tenant(
                name=body.name,
                api_key_hash=hash_api_key(api_key),
                daily_send_cap=body.daily_send_cap,
                monthly_spend_cap_units=body.monthly_spend_cap_units,
                sender_from_address=(
                    str(body.sender_from_address) if body.sender_from_address else None
                ),
            )
            session.add(tenant)
            session.flush()
            source = LeadSourceRegister(tenant_id=tenant.id, **body.source.model_dump())
            campaign = Campaign(tenant_id=tenant.id, **body.campaign.model_dump())
            session.add_all([source, campaign])
            session.flush()
            audit.record(
                session,
                tenant_id=tenant.id,
                actor_type="human",
                actor_id="admin",
                action="tenant.onboard",
                entity_type="tenant",
                entity_id=str(tenant.id),
                payload={
                    "source_id": str(source.id),
                    "campaign_id": str(campaign.id),
                    "daily_send_cap": body.daily_send_cap,
                    "monthly_spend_cap_units": body.monthly_spend_cap_units,
                },
            )
            response = schemas.TenantOnboardResponse(
                tenant_id=tenant.id,
                name=tenant.name,
                api_key=api_key,
                source_id=source.id,
                campaign_id=campaign.id,
                daily_send_cap=body.daily_send_cap,
                monthly_spend_cap_units=body.monthly_spend_cap_units,
                sender_from_address=tenant.sender_from_address,
            )
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"onboarding conflict: {str(exc.orig)[:200]}",
        ) from exc
    return response


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
                retention_until=body.retention_until,
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
                    "email": str(body.email),  # deny-key: audit stores [REDACTED]
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
    try:
        runner = PipelineRunner(
            tenant_id,
            lead_id=lead_id,
            max_iterations=body.max_iterations,
            budget_units=body.budget_units,
        )
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
        except (ApprovalError, TransitionError, IntegrityError) as exc:
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
        except (ApprovalError, TransitionError, IntegrityError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return {"status": "rejected"}


@router.get(
    "/outreach-drafts/pending",
    response_model=schemas.PendingDraftsResponse,
)
def pending_drafts(
    tenant_id: uuid.UUID = Depends(require_tenant),
) -> schemas.PendingDraftsResponse:
    """The reviewer's queue: drafts waiting at the human gate.

    Confidence-ordered (highest fit score first, FIFO within a score):
    the top of the queue is the batchable tail, the bottom is where
    reviewer attention belongs.
    """
    with tenant_session(tenant_id) as session:
        rows = session.execute(
            select(OutreachDraft, Lead)
            .join(
                Lead,
                (Lead.tenant_id == OutreachDraft.tenant_id)
                & (Lead.id == OutreachDraft.lead_id),
            )
            .where(OutreachDraft.status == "pending_approval")
            .order_by(Lead.fit_score.desc().nulls_last(), OutreachDraft.created_at)
        ).all()
        return schemas.PendingDraftsResponse(
            drafts=[
                schemas.PendingDraftItem(
                    draft_id=draft.id,
                    lead_id=lead.id,
                    campaign_id=draft.campaign_id,
                    version=draft.version,
                    subject=draft.subject,
                    body=draft.body,
                    personalization_sources=draft.personalization_sources or {},
                    lead_first_name=lead.first_name,
                    lead_company=lead.company_name,
                    lead_state=lead.state,
                    fit_score=(
                        float(lead.fit_score) if lead.fit_score is not None else None
                    ),
                    created_at=draft.created_at,
                )
                for draft, lead in rows
            ]
        )


@router.post(
    "/outreach-drafts/{draft_id}/review",
    response_model=schemas.ReviewResponse,
)
def review(
    draft_id: uuid.UUID,
    body: schemas.ReviewRequest,
    tenant_id: uuid.UUID = Depends(require_tenant),
) -> schemas.ReviewResponse:
    """The rubric review endpoint — approve / approve-with-edits / reject.

    Like /approve, this NEVER sends; it only moves content through the
    human gate with a recorded, append-only rubric decision.
    """
    with tenant_session(tenant_id) as session:
        draft = session.get(OutreachDraft, draft_id)
        if draft is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "draft not found")
        try:
            outcome = review_draft(
                session,
                draft=draft,
                reviewer=body.reviewer,
                decision=body.decision,
                reasons=body.reasons,
                notes=body.notes,
                edited_subject=body.edited_subject,
                edited_body=body.edited_body,
            )
        except (ApprovalError, ValueError, TransitionError, IntegrityError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        lead = session.get(Lead, draft.lead_id)
        assert lead is not None
        return schemas.ReviewResponse(
            review_id=outcome.review_id,
            draft_id=draft_id,
            decision=outcome.decision,
            active_draft_id=outcome.active_draft_id,
            lead_state=lead.state,
        )


@router.post(
    "/outreach-drafts/batch-review",
    response_model=schemas.BatchReviewResponse,
)
def batch_review(
    body: schemas.BatchReviewRequest,
    tenant_id: uuid.UUID = Depends(require_tenant),
) -> schemas.BatchReviewResponse:
    """Review many drafts in one call (Phase 3: human-in-the-loop at scale).

    Each item is processed in ITS OWN transaction through the same rubric
    path as the single-draft endpoint — one bad item (stale draft, wrong
    state) fails alone and the rest of the batch still lands. Like every
    review surface, this never sends.
    """
    results: list[schemas.BatchReviewResultItem] = []
    counts = {"approved": 0, "approved_with_edits": 0, "rejected": 0}
    failed = 0
    for item in body.items:
        try:
            with tenant_session(tenant_id) as session:
                draft = session.get(OutreachDraft, item.draft_id)
                if draft is None:
                    raise ApprovalError("draft not found")
                outcome = review_draft(
                    session,
                    draft=draft,
                    reviewer=body.reviewer,
                    decision=item.decision,
                    reasons=item.reasons,
                    notes=item.notes,
                    edited_subject=item.edited_subject,
                    edited_body=item.edited_body,
                )
                lead = session.get(Lead, draft.lead_id)
                results.append(
                    schemas.BatchReviewResultItem(
                        draft_id=item.draft_id,
                        ok=True,
                        decision=item.decision,
                        active_draft_id=outcome.active_draft_id,
                        lead_state=lead.state if lead else None,
                    )
                )
                counts[str(item.decision)] += 1
        except (ApprovalError, ValueError, TransitionError, IntegrityError) as exc:
            failed += 1
            results.append(
                schemas.BatchReviewResultItem(
                    draft_id=item.draft_id,
                    ok=False,
                    decision=item.decision,
                    error=str(exc)[:500],
                )
            )
    log.info(
        "batch review processed",
        reviewer=body.reviewer,
        approved=counts["approved"],
        edited=counts["approved_with_edits"],
        rejected=counts["rejected"],
        failed=failed,
    )
    return schemas.BatchReviewResponse(
        results=results,
        approved=counts["approved"],
        edited=counts["approved_with_edits"],
        rejected=counts["rejected"],
        failed=failed,
    )


# ── The approval UI: a static page, credentials stay client-side ────────────


@router.get("/review", include_in_schema=False)
def review_page() -> HTMLResponse:
    from relay.api.review_ui import REVIEW_PAGE

    return HTMLResponse(REVIEW_PAGE)


@router.get("/admin", include_in_schema=False)
def admin_page() -> HTMLResponse:
    """The admin console: a static page over the admin API. Serving it
    is harmless without the admin token — every action it can take is
    token-gated server-side, exactly like curl."""
    from relay.api.admin_ui import ADMIN_PAGE

    return HTMLResponse(ADMIN_PAGE)


# ── Economics gate (Phase 1A) ────────────────────────────────────────────────


@router.get(
    "/campaigns/{campaign_id}/economics",
    response_model=schemas.EconomicsResponse,
)
def economics(
    campaign_id: uuid.UUID,
    tenant_id: uuid.UUID = Depends(require_tenant),
) -> schemas.EconomicsResponse:
    with tenant_session(tenant_id) as session:
        if session.get(Campaign, campaign_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "campaign not found")
    report = campaign_economics(tenant_id, campaign_id)
    return schemas.EconomicsResponse(
        campaign_id=report.campaign_id,
        funnel=report.funnel,
        cost_units_total=report.cost_units_total,
        cost_units_per_meeting=report.cost_units_per_meeting,
        cost_usd_per_meeting=report.cost_usd_per_meeting,
    )


# ── Observability (Phase 2) ─────────────────────────────────────────────────


@router.get("/economics", response_model=schemas.TenantEconomicsResponse)
def economics_tenant(
    tenant_id: uuid.UUID = Depends(require_tenant),
) -> schemas.TenantEconomicsResponse:
    """Phase 4 cost attribution: the client-profitability view across all
    of this tenant's campaigns, plus headroom under the monthly cap."""
    report = tenant_economics(tenant_id)
    return schemas.TenantEconomicsResponse(
        tenant_id=report.tenant_id,
        funnel=report.funnel,
        cost_units_total=report.cost_units_total,
        cost_units_30d=report.cost_units_30d,
        cost_units_per_meeting=report.cost_units_per_meeting,
        cost_usd_per_meeting=report.cost_usd_per_meeting,
        monthly_spend_cap_units=report.monthly_spend_cap_units,
        spend_cap_remaining_units=report.spend_cap_remaining_units,
    )


@router.get("/metrics", response_model=schemas.MetricsResponse)
def metrics_json(
    tenant_id: uuid.UUID = Depends(require_tenant),
) -> schemas.MetricsResponse:
    m = tenant_metrics(tenant_id)
    return schemas.MetricsResponse(
        tenant_id=m.tenant_id,
        generated_at=m.generated_at,
        lead_states=m.lead_states,
        runs_window=m.runs,
        cost_units_window=m.cost_units_window,
        send_jobs=m.send_jobs,
        replies_window=m.replies_window,
        sent_window=m.sent_window,
        suppression_entries=m.suppression_entries,
        suppressions_window=m.suppressions_window,
        reviews_window=m.reviews_window,
        run_error_rate=m.run_error_rate,
        reply_rate=m.reply_rate,
        bounce_rate=m.bounce_rate,
        complaint_rate=m.complaint_rate,
        edit_rate=m.edit_rate,
    )


@router.get("/metrics/prometheus", include_in_schema=False)
def metrics_prometheus(
    tenant_id: uuid.UUID = Depends(require_tenant),
):
    from fastapi.responses import PlainTextResponse

    return PlainTextResponse(prometheus_text(tenant_metrics(tenant_id)))


@router.get("/alerts", response_model=schemas.AlertsResponse)
def alerts(
    tenant_id: uuid.UUID = Depends(require_tenant),
) -> schemas.AlertsResponse:
    fired = evaluate_alerts(tenant_id)
    return schemas.AlertsResponse(
        tenant_id=tenant_id,
        alerts=[
            schemas.AlertItem(
                rule=a.rule, severity=a.severity, detail=a.detail, value=a.value
            )
            for a in fired
        ],
    )


@router.get("/ops", include_in_schema=False)
def ops_page() -> HTMLResponse:
    from relay.api.ops_ui import OPS_PAGE

    return HTMLResponse(OPS_PAGE)


# ── DSR erasure (tenant-scoped): the right to be forgotten ──────────────────


@router.post("/dsr/erasure", response_model=schemas.ErasureResponse)
def dsr_erasure(
    body: schemas.ErasureRequest,
    tenant_id: uuid.UUID = Depends(require_tenant),
) -> schemas.ErasureResponse:
    """Erase a person's data (datastore + CRM), leaving only the hashed
    do-not-contact entry. Idempotent: erasing an unknown address still
    records the suppression — a DSR is honored even for someone never
    ingested."""
    result = dsr.execute_erasure(
        tenant_id, email=str(body.email), requested_by=body.requested_by
    )
    return schemas.ErasureResponse(
        email_hash=result.email_hash,
        lead_ids=[uuid.UUID(x) for x in result.lead_ids],
        datastore=result.datastore,
        crm=result.crm,
        vector_store=result.vector_store,
        suppression_added=result.suppression_added,
    )


# ── Internal: the Legal/Data Preflight gate (admin token) ───────────────────
# Approval opens real-data ingestion for a tenant; it is deliberately NOT
# reachable with a tenant API key — the gate is operated by whoever owns
# compliance, above any single tenant integration.


@router.post(
    "/internal/preflight/approve",
    response_model=schemas.PreflightStatusResponse,
    dependencies=[Depends(require_admin)],
)
def preflight_approve(
    body: schemas.PreflightApproveRequest,
) -> schemas.PreflightStatusResponse:
    try:
        preflight.approve(
            body.tenant_id,
            artifact_sha256=body.artifact_sha256.lower(),
            approved_by=body.approved_by,
            artifact_ref=body.artifact_ref,
            notes=body.notes,
        )
    except (preflight.PreflightError, IntegrityError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return _preflight_status(body.tenant_id)


@router.post(
    "/internal/preflight/revoke",
    response_model=schemas.PreflightStatusResponse,
    dependencies=[Depends(require_admin)],
)
def preflight_revoke(
    body: schemas.PreflightRevokeRequest,
) -> schemas.PreflightStatusResponse:
    try:
        preflight.revoke(body.tenant_id, revoked_by=body.revoked_by, reason=body.reason)
    except preflight.PreflightError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return _preflight_status(body.tenant_id)


@router.get(
    "/internal/preflight/{tenant_id}",
    response_model=schemas.PreflightStatusResponse,
    dependencies=[Depends(require_admin)],
)
def preflight_status(tenant_id: uuid.UUID) -> schemas.PreflightStatusResponse:
    return _preflight_status(tenant_id)


def _preflight_status(tenant_id: uuid.UUID) -> schemas.PreflightStatusResponse:
    record = preflight.get_record(tenant_id)
    if record is None:
        return schemas.PreflightStatusResponse(tenant_id=tenant_id, approved=False)
    return schemas.PreflightStatusResponse(
        tenant_id=tenant_id,
        approved=record.revoked_at is None,
        artifact_sha256=record.artifact_sha256,
        artifact_ref=record.artifact_ref,
        approved_by=record.approved_by,
        approved_at=record.approved_at,
        revoked_at=record.revoked_at,
    )


# ── Provider webhooks (Phase 1C): SNS → SES events ─────────────────────────
# SNS cannot send custom headers, so authentication is a token in the
# URL, compared constant-time against configuration. Every envelope is
# additionally signature-verified against the AWS signing certificate
# before any content is trusted.


@router.post("/webhooks/ses", include_in_schema=False)
async def ses_webhook(request: Request, token: str = "") -> dict[str, int]:
    settings_token = get_settings().ses_webhook_token
    if settings_token is None or not secrets.compare_digest(
        token, settings_token.get_secret_value()
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "bad webhook token")
    body = await request.body()
    try:
        stats = process_sns_envelope(body)
    except EventRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {
        "bounces": stats.bounces,
        "complaints": stats.complaints,
        "deliveries": stats.deliveries,
        "ignored": stats.ignored,
    }


# ── One-click unsubscribe (RFC 8058) — token-authenticated, no API key ──────

_UNSUBSCRIBE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Unsubscribe</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 32rem;
             margin: 4rem auto; padding: 0 1rem;">
<h1 style="font-size:1.3rem">Unsubscribe</h1>
<p>Click the button below to stop receiving emails from this sender.</p>
<form method="post"><button type="submit"
  style="padding:.6rem 1.2rem; font-size:1rem; cursor:pointer;">
  Unsubscribe</button></form>
</body></html>"""

_UNSUBSCRIBED_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Unsubscribed</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 32rem;
             margin: 4rem auto; padding: 0 1rem;">
<h1 style="font-size:1.3rem">You have been unsubscribed</h1>
<p>You will not receive further emails from this sender.</p>
</body></html>"""


@router.get("/unsubscribe", include_in_schema=False)
def unsubscribe_page(token: str = "") -> HTMLResponse:
    """Human-facing landing page. NEVER mutates state — mail clients and
    security scanners prefetch GET links; only the POST acts."""
    try:
        verify_token(token)
    except UnsubscribeRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return HTMLResponse(_UNSUBSCRIBE_PAGE)


@router.post("/unsubscribe", include_in_schema=False)
def unsubscribe_submit(token: str = "") -> HTMLResponse:
    """The acting endpoint: an RFC 8058 one-click POST from the mail
    provider, or the human confirming on the GET page. Idempotent."""
    try:
        process_unsubscribe(token)
    except UnsubscribeRejected as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return HTMLResponse(_UNSUBSCRIBED_PAGE)


# ── Internal: spine-triggered worker tick (admin token, not tenant key) ─────


@router.post(
    "/internal/send-worker/tick",
    response_model=schemas.WorkerTickResponse,
    dependencies=[Depends(require_admin)],
)
def send_worker_tick() -> schemas.WorkerTickResponse:
    stats = process_pending()
    return schemas.WorkerTickResponse(
        sent=stats.sent,
        blocked=stats.blocked,
        failed=stats.failed,
        deferred=stats.deferred,
    )
