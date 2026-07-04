"""API request/response models. Typed at the boundary (§11): model and
tool output must conform to schemas — so must every HTTP payload."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from email_validator import EmailNotValidError, validate_email
from pydantic import AfterValidator, BaseModel, Field

from relay.domain.vocab import LawfulBasis, ReviewDecision, ReviewReason


def _validated_email(value: str) -> str:
    """Email validation that ACCEPTS special-use test domains (.test).

    Phase 0 is synthetic-only: addresses under reserved TLDs are a
    feature — they are structurally incapable of belonging to a real
    person. Phase 1B (real-data pilot) tightens this to strict
    validation behind the legal gate.
    """
    try:
        result = validate_email(
            value, check_deliverability=False, test_environment=True
        )
    except EmailNotValidError as exc:
        raise ValueError(str(exc)) from exc
    return result.normalized


EmailAddress = Annotated[str, AfterValidator(_validated_email)]

# ── Tenants ─────────────────────────────────────────────────────────────────


class TenantCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class TenantCreateResponse(BaseModel):
    id: uuid.UUID
    name: str
    #: Shown exactly once; only the hash is stored.
    api_key: str


# ── Lead source register (§7) ───────────────────────────────────────────────


class SourceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    source_type: Literal[
        "synthetic",
        "seed",
        "api",
        "uploaded_list",
        "licensed_provider",
        "crm_import",
        "public_registry",
        "website",
    ]
    terms_allow_use: Literal["yes", "no", "legal_review_needed"]
    personal_data_collected: list[str] = Field(default_factory=list)
    region_restrictions: list[str] = Field(default_factory=list)
    proof_of_lawful_use: str | None = None
    deletion_mechanism: str | None = None
    confidence_score: float = Field(default=1.0, ge=0, le=1)
    suppression_checked_before_import: bool = False


class SourceResponse(BaseModel):
    id: uuid.UUID
    name: str
    source_type: str
    terms_allow_use: str


# ── Campaigns ───────────────────────────────────────────────────────────────


class CampaignCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    dry_run: bool = True
    simulated_replies_enabled: bool = False
    mailbox_id: str | None = None
    daily_volume_cap: int | None = Field(default=None, ge=1)


class CampaignResponse(BaseModel):
    id: uuid.UUID
    name: str
    dry_run: bool
    simulated_replies_enabled: bool
    status: str


# ── Leads ───────────────────────────────────────────────────────────────────


class LeadCreateRequest(BaseModel):
    campaign_id: uuid.UUID
    source_id: uuid.UUID
    email: EmailAddress
    lawful_basis: LawfulBasis
    region_assumption: str = Field(min_length=2, max_length=64)
    dry_run: bool = True
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    company_name: str | None = None
    company_domain: str | None = None


class LeadResponse(BaseModel):
    id: uuid.UUID
    campaign_id: uuid.UUID
    state: str
    dry_run: bool
    email_verified: bool
    lawful_basis: str
    region_assumption: str
    fit_score: float | None
    approved_message_version: int | None


class TraceEntry(BaseModel):
    from_state: str
    to_state: str
    actor: str
    reason: str | None
    run_id: uuid.UUID | None
    created_at: datetime


class LeadTraceResponse(BaseModel):
    lead_id: uuid.UUID
    state: str
    transitions: list[TraceEntry]


# ── Pipeline runs ───────────────────────────────────────────────────────────


class RunRequest(BaseModel):
    max_iterations: int | None = Field(default=None, ge=1, le=10_000)
    budget_units: float | None = Field(default=None, gt=0)


class RunResponse(BaseModel):
    run_id: uuid.UUID
    lead_id: uuid.UUID
    final_state: str
    steps: int
    cost_units: float
    stopped_on: str
    visited: list[str]


# ── Human gate ──────────────────────────────────────────────────────────────


class ApproveRequest(BaseModel):
    approver: str = Field(min_length=1, max_length=200)


class RejectRequest(BaseModel):
    approver: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=2000)


class ApproveResponse(BaseModel):
    draft_id: uuid.UUID
    version: int
    approved: bool
    #: Always false here: approval never sends (§10).
    sent: Literal[False] = False
    lead_state: str


# ── Campaign status ─────────────────────────────────────────────────────────


class CampaignStatusResponse(BaseModel):
    campaign_id: uuid.UUID
    name: str
    dry_run: bool
    lead_states: dict[str, int]
    send_jobs: dict[str, int]


# ── Internal worker tick ────────────────────────────────────────────────────


class WorkerTickResponse(BaseModel):
    sent: int
    blocked: int
    failed: int


# ── Rubric review (Phase 1A human gate) ─────────────────────────────────────


class ReviewRequest(BaseModel):
    reviewer: str = Field(min_length=1, max_length=200)
    decision: ReviewDecision
    reasons: list[ReviewReason] = Field(default_factory=list)
    notes: str | None = Field(default=None, max_length=2000)
    edited_subject: str | None = Field(default=None, max_length=200)
    edited_body: str | None = Field(default=None, max_length=5000)


class ReviewResponse(BaseModel):
    review_id: uuid.UUID
    draft_id: uuid.UUID
    decision: ReviewDecision
    #: The draft that is approved after this review (None unless approved).
    active_draft_id: uuid.UUID | None
    #: Always false here: review/approval never sends (§10).
    sent: Literal[False] = False
    lead_state: str


class PendingDraftItem(BaseModel):
    draft_id: uuid.UUID
    lead_id: uuid.UUID
    campaign_id: uuid.UUID
    version: int
    subject: str
    body: str
    personalization_sources: dict
    lead_first_name: str | None
    lead_company: str | None
    lead_state: str
    created_at: datetime


class PendingDraftsResponse(BaseModel):
    drafts: list[PendingDraftItem]


# ── Economics (Phase 1A gate) ───────────────────────────────────────────────


class EconomicsResponse(BaseModel):
    campaign_id: uuid.UUID
    funnel: dict[str, int]
    cost_units_total: float
    cost_units_per_meeting: float | None
    #: Omitted (None) unless RELAY_COST_UNIT_USD is calibrated.
    cost_usd_per_meeting: float | None
