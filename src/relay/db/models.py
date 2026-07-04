"""Canonical datastore schema — tenant-aware from the first migration.

Design rules (project documentation §3, §4, §7, §10):

- every tenant-owned table carries ``tenant_id``; row-level security is
  FORCEd for the application role (sql/004_rls.sql);
- child rows reference parents with *composite* foreign keys that include
  ``tenant_id``, so a cross-tenant link is structurally impossible;
- states, suppression scopes, reasons etc. are TEXT + CHECK constraints
  (kept in lockstep with the Python enums by tests);
- the duplicate-send guard is a database UNIQUE constraint, not app logic;
- the audit log is append-only (trigger-enforced).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from relay.domain.states import LeadState


class Base(DeclarativeBase):
    type_annotation_map = {
        uuid.UUID: UUID(as_uuid=True),
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
    }


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))


def _created_at() -> Mapped[datetime]:
    return mapped_column(nullable=False, server_default=text("now()"))


_STATES_SQL = ", ".join(f"'{s}'" for s in LeadState)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # Only the hash is stored; the raw key is shown once at bootstrap.
    api_key_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = _created_at()


class LeadSourceRegister(Base):
    """Lead Source Register (§7): provenance and lawful-use record per source.

    Hard rule enforced downstream: no lead exists without a register entry
    whose terms allow the use.
    """

    __tablename__ = "lead_source_register"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_sources_tenant_id_id"),
        UniqueConstraint("tenant_id", "name", name="uq_sources_tenant_name"),
        CheckConstraint(
            "source_type IN ('synthetic','seed','api','uploaded_list',"
            "'licensed_provider','crm_import','public_registry','website')",
            name="ck_sources_source_type",
        ),
        CheckConstraint(
            "terms_allow_use IN ('yes','no','legal_review_needed')",
            name="ck_sources_terms_allow_use",
        ),
        CheckConstraint(
            "confidence_score >= 0 AND confidence_score <= 1",
            name="ck_sources_confidence",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    terms_allow_use: Mapped[str] = mapped_column(Text, nullable=False)
    personal_data_collected: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    region_restrictions: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    proof_of_lawful_use: Mapped[str | None] = mapped_column(Text)
    deletion_mechanism: Mapped[str | None] = mapped_column(Text)
    confidence_score: Mapped[float] = mapped_column(
        Numeric(3, 2), nullable=False, server_default=text("1.0")
    )
    suppression_checked_before_import: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at: Mapped[datetime] = _created_at()


class Campaign(Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_campaigns_tenant_id_id"),
        UniqueConstraint("tenant_id", "name", name="uq_campaigns_tenant_name"),
        CheckConstraint(
            "status IN ('draft','active','paused','completed')",
            name="ck_campaigns_status",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # dry_run is first-class and immutable (trigger); safe default: true.
    dry_run: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    # Explicit seed/test mode: only then may dry-run leads "receive" replies.
    simulated_replies_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    mailbox_id: Mapped[str | None] = mapped_column(Text)
    daily_volume_cap: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )
    created_at: Mapped[datetime] = _created_at()


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_leads_tenant_id_id"),
        # Deduplication guardrail: one lead per address per campaign.
        UniqueConstraint(
            "tenant_id",
            "campaign_id",
            "email_hash",
            name="uq_leads_campaign_email",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["campaigns.tenant_id", "campaigns.id"],
            name="fk_leads_campaign_same_tenant",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "source_id"],
            ["lead_source_register.tenant_id", "lead_source_register.id"],
            name="fk_leads_source_same_tenant",
        ),
        CheckConstraint(f"state IN ({_STATES_SQL})", name="ck_leads_state"),
        # Hard rule (§7): a lead may only exist with terms that allow use.
        CheckConstraint(
            "source_terms_status = 'yes'", name="ck_leads_source_terms_yes"
        ),
        CheckConstraint(
            "lawful_basis IN ('synthetic','test_consent','consent','contract',"
            "'legitimate_interest','client_warranty')",
            name="ck_leads_lawful_basis",
        ),
        CheckConstraint("retry_count >= 0", name="ck_leads_retry_count"),
        CheckConstraint("max_retries >= 0", name="ck_leads_max_retries"),
        CheckConstraint(
            f"error_return_state IS NULL OR error_return_state IN ({_STATES_SQL})",
            name="ck_leads_error_return_state",
        ),
        Index("ix_leads_tenant_state", "tenant_id", "state"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(nullable=False)

    # ── Provenance (§7 hard rule: all four NOT NULL) ────────────────────────
    source_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    source_terms_status: Mapped[str] = mapped_column(Text, nullable=False)
    lawful_basis: Mapped[str] = mapped_column(Text, nullable=False)
    region_assumption: Mapped[str] = mapped_column(Text, nullable=False)
    # Data-retention field; the deletion workflow lands in Phase 1B.
    retention_until: Mapped[datetime | None] = mapped_column()

    # ── Contact ─────────────────────────────────────────────────────────────
    email: Mapped[str] = mapped_column(Text, nullable=False)
    email_hash: Mapped[str] = mapped_column(Text, nullable=False)
    email_domain: Mapped[str] = mapped_column(Text, nullable=False)
    email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    first_name: Mapped[str | None] = mapped_column(Text)
    last_name: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    company_name: Mapped[str | None] = mapped_column(Text)
    company_domain: Mapped[str | None] = mapped_column(Text)

    # ── Pipeline state ──────────────────────────────────────────────────────
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'created'")
    )
    # First-class dry-run flag; immutable after insert (trigger).
    dry_run: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    max_retries: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3")
    )
    error_return_state: Mapped[str | None] = mapped_column(Text)
    fit_score: Mapped[float | None] = mapped_column(Numeric(4, 3))
    approved_message_version: Mapped[int | None] = mapped_column(Integer)
    replied_at: Mapped[datetime | None] = mapped_column()
    booking_ref: Mapped[str | None] = mapped_column(Text)
    unsubscribed_at: Mapped[datetime | None] = mapped_column()

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("now()")
    )


class LeadTransition(Base):
    """Append-only trace of every state change — the per-lead journey log."""

    __tablename__ = "lead_transitions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "lead_id"],
            ["leads.tenant_id", "leads.id"],
            name="fk_transitions_lead_same_tenant",
        ),
        CheckConstraint(
            f"from_state IN ({_STATES_SQL})", name="ck_transitions_from_state"
        ),
        CheckConstraint(f"to_state IN ({_STATES_SQL})", name="ck_transitions_to_state"),
        Index("ix_transitions_lead", "tenant_id", "lead_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    from_state: Mapped[str] = mapped_column(Text, nullable=False)
    to_state: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[uuid.UUID | None] = mapped_column()
    created_at: Mapped[datetime] = _created_at()


class TransitionRule(Base):
    """Legal (from → to) edges, seeded from relay.domain.states at migrate.

    Reference data, not tenant-scoped. The BEFORE UPDATE trigger on leads
    rejects any UPDATE whose (old, new) pair is absent here.
    """

    __tablename__ = "lead_transition_rules"

    from_state: Mapped[str] = mapped_column(Text, primary_key=True)
    to_state: Mapped[str] = mapped_column(Text, primary_key=True)


class Suppression(Base):
    """The authoritative do-not-contact set (§10 Suppression Contract)."""

    __tablename__ = "suppression"
    __table_args__ = (
        CheckConstraint(
            "scope IN ('tenant','global','domain','mailbox','campaign')",
            name="ck_suppression_scope",
        ),
        CheckConstraint(
            "reason IN ('unsubscribe','complaint','hard_bounce','manual',"
            "'legal_delete','do_not_contact')",
            name="ck_suppression_reason",
        ),
        CheckConstraint(
            "source IN ('reply','link','crm','manual','provider_webhook',"
            "'import','system')",
            name="ck_suppression_source",
        ),
        # Scope ⇒ required discriminator fields.
        CheckConstraint(
            "(scope NOT IN ('tenant','global') OR email_hash IS NOT NULL) AND "
            "(scope <> 'domain' OR domain IS NOT NULL) AND "
            "(scope <> 'campaign' OR (campaign_id IS NOT NULL "
            "AND email_hash IS NOT NULL)) AND "
            "(scope <> 'mailbox' OR (mailbox_id IS NOT NULL "
            "AND email_hash IS NOT NULL))",
            name="ck_suppression_scope_fields",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["campaigns.tenant_id", "campaigns.id"],
            name="fk_suppression_campaign_same_tenant",
        ),
        Index("ix_suppression_email_hash", "email_hash"),
        Index("ix_suppression_tenant_email", "tenant_id", "email_hash"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    email_hash: Mapped[str | None] = mapped_column(Text)
    # Encrypted raw address (tenant-derived key). Populated from Phase 1B
    # when real addresses exist; hash-only is fine for synthetic data.
    raw_email_encrypted: Mapped[bytes | None] = mapped_column(BYTEA)
    domain: Mapped[str | None] = mapped_column(Text)
    mailbox_id: Mapped[str | None] = mapped_column(Text)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column()
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column()
    applies_to_marketing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    applies_to_sales: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    created_at: Mapped[datetime] = _created_at()


class OutreachDraft(Base):
    __tablename__ = "outreach_drafts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_drafts_tenant_id_id"),
        UniqueConstraint(
            "tenant_id", "lead_id", "version", name="uq_drafts_lead_version"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "lead_id"],
            ["leads.tenant_id", "leads.id"],
            name="fk_drafts_lead_same_tenant",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["campaigns.tenant_id", "campaigns.id"],
            name="fk_drafts_campaign_same_tenant",
        ),
        CheckConstraint(
            "status IN ('draft','pending_approval','approved','rejected')",
            name="ck_drafts_status",
        ),
        CheckConstraint("version >= 1", name="ck_drafts_version"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    campaign_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Provenance of the facts used (§11): reviewers audit what fed the copy.
    personalization_sources: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'draft'")
    )
    review_reason: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = _created_at()


class SendJob(Base):
    """Transactional outbox for sends. The most defended table in RELAY.

    - the idempotency key is a UNIQUE constraint: duplicate sends are
      impossible even under replayed webhooks, workflow bugs, or races;
    - a partial unique index allows at most one active send per lead;
    - the BEFORE trigger re-checks suppression, dry-run, and approval on
      INSERT and again when a worker claims the job (status → 'sending').
    """

    __tablename__ = "send_jobs"
    __table_args__ = (
        # THE duplicate-send guard (§10).
        UniqueConstraint(
            "tenant_id",
            "campaign_id",
            "lead_id",
            "sequence_step",
            "message_version",
            name="uq_send_jobs_idempotency",
        ),
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_send_jobs_idempotency_key"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "lead_id"],
            ["leads.tenant_id", "leads.id"],
            name="fk_send_jobs_lead_same_tenant",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["campaigns.tenant_id", "campaigns.id"],
            name="fk_send_jobs_campaign_same_tenant",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "draft_id"],
            ["outreach_drafts.tenant_id", "outreach_drafts.id"],
            name="fk_send_jobs_draft_same_tenant",
        ),
        CheckConstraint("mode IN ('simulated','real')", name="ck_send_jobs_mode"),
        CheckConstraint(
            "status IN ('queued','sending','sent','failed','blocked')",
            name="ck_send_jobs_status",
        ),
        CheckConstraint("sequence_step >= 1", name="ck_send_jobs_step"),
        # A lead cannot be in two active campaign send states simultaneously.
        Index(
            "uq_send_jobs_one_active_per_lead",
            "tenant_id",
            "lead_id",
            unique=True,
            postgresql_where=text("status IN ('queued','sending')"),
        ),
        Index("ix_send_jobs_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    campaign_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    lead_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    draft_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    sequence_step: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    message_version: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'queued'")
    )
    recipient_email_hash: Mapped[str] = mapped_column(Text, nullable=False)
    recipient_domain: Mapped[str] = mapped_column(Text, nullable=False)
    mailbox_id: Mapped[str | None] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    queued_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("now()")
    )
    started_at: Mapped[datetime | None] = mapped_column()
    completed_at: Mapped[datetime | None] = mapped_column()


class AuditLog(Base):
    """Append-only audit trail. UPDATE/DELETE raise via trigger."""

    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('system','human','planner','worker')",
            name="ck_audit_actor_type",
        ),
        Index("ix_audit_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text)
    # Redacted before insert (relay.logs.redact_payload).
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = _created_at()


class PipelineRun(Base):
    """One guardrailed execution: iteration counter + budget, in the DB."""

    __tablename__ = "pipeline_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running','completed','killed_iteration_cap',"
            "'killed_budget','failed')",
            name="ck_runs_status",
        ),
        CheckConstraint("max_iterations >= 1", name="ck_runs_max_iterations"),
        CheckConstraint("budget_units > 0", name="ck_runs_budget"),
        Index("ix_runs_tenant_started", "tenant_id", "started_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    lead_id: Mapped[uuid.UUID | None] = mapped_column()
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'running'")
    )
    iterations: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    max_iterations: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_units: Mapped[float] = mapped_column(
        Numeric(12, 4), nullable=False, server_default=text("0")
    )
    budget_units: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column()


#: Tables that carry tenant_id and get RLS + tenant-immutability triggers.
TENANT_TABLES: tuple[str, ...] = (
    "lead_source_register",
    "campaigns",
    "leads",
    "lead_transitions",
    "suppression",
    "outreach_drafts",
    "send_jobs",
    "audit_log",
    "pipeline_runs",
)
