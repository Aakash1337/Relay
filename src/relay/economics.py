"""The economics gate (Phase 1A) — cost per qualified meeting, from data.

Every number here is derived from rows the pipeline already writes
(transitions, runs, send jobs, reviews); there is no separate metering
to drift out of sync. Costs are in guardrail units — the same units the
budget ceiling enforces — with an optional configured USD rate for the
projection.

The Phase 1A exit question this answers: "does the funnel's unit
economics survive contact with (synthetic) reality before we spend
money on real data and sending?"
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import distinct, func, select

from relay.config import get_settings
from relay.db.engine import tenant_session
from relay.db.models import (
    DraftReview,
    Lead,
    LeadTransition,
    PipelineRun,
    Reply,
    SendJob,
    Tenant,
)


@dataclass(frozen=True)
class EconomicsReport:
    campaign_id: uuid.UUID
    leads_total: int
    leads_qualified: int
    drafts_reviewed: int
    drafts_approved: int
    sends_completed: int
    replies_received: int
    interested: int
    meetings_booked: int
    cost_units_total: float
    #: units per booked meeting; None until the first booking.
    cost_units_per_meeting: float | None
    #: USD projection; None unless RELAY_COST_UNIT_USD is configured.
    cost_usd_per_meeting: float | None

    @property
    def funnel(self) -> dict[str, int]:
        return {
            "leads": self.leads_total,
            "qualified": self.leads_qualified,
            "reviewed": self.drafts_reviewed,
            "approved": self.drafts_approved,
            "sent": self.sends_completed,
            "replied": self.replies_received,
            "interested": self.interested,
            "booked": self.meetings_booked,
        }


def _count_reached(session, campaign_id: uuid.UUID, state: str) -> int:
    """Leads of this campaign that ever transitioned into ``state`` —
    robust to where they ended up afterwards."""
    return session.execute(
        select(func.count(distinct(LeadTransition.lead_id)))
        .join(
            Lead,
            (Lead.tenant_id == LeadTransition.tenant_id)
            & (Lead.id == LeadTransition.lead_id),
        )
        .where(
            Lead.campaign_id == campaign_id,
            LeadTransition.to_state == state,
        )
    ).scalar_one()


def campaign_economics(tenant_id: uuid.UUID, campaign_id: uuid.UUID) -> EconomicsReport:
    settings = get_settings()
    with tenant_session(tenant_id) as session:
        leads_total = session.execute(
            select(func.count()).where(Lead.campaign_id == campaign_id)
        ).scalar_one()
        qualified = _count_reached(session, campaign_id, "scored_qualified")
        interested = _count_reached(session, campaign_id, "interested")
        booked = _count_reached(session, campaign_id, "booked")

        drafts_reviewed = session.execute(
            select(func.count())
            .select_from(DraftReview)
            .join(
                Lead,
                (Lead.tenant_id == DraftReview.tenant_id)
                & (Lead.id == DraftReview.lead_id),
            )
            .where(Lead.campaign_id == campaign_id)
        ).scalar_one()
        drafts_approved = _count_reached(session, campaign_id, "approved")

        sends = session.execute(
            select(func.count()).where(
                SendJob.campaign_id == campaign_id, SendJob.status == "sent"
            )
        ).scalar_one()
        replies = session.execute(
            select(func.count()).where(Reply.campaign_id == campaign_id)
        ).scalar_one()

        cost_units = float(
            session.execute(
                select(func.coalesce(func.sum(PipelineRun.cost_units), 0)).where(
                    PipelineRun.lead_id.in_(
                        select(Lead.id).where(Lead.campaign_id == campaign_id)
                    )
                )
            ).scalar_one()
        )

    per_meeting = (cost_units / booked) if booked else None
    usd_rate = settings.cost_unit_usd
    usd_per_meeting = (
        per_meeting * usd_rate if (per_meeting is not None and usd_rate > 0) else None
    )
    return EconomicsReport(
        campaign_id=campaign_id,
        leads_total=leads_total,
        leads_qualified=qualified,
        drafts_reviewed=drafts_reviewed,
        drafts_approved=drafts_approved,
        sends_completed=sends,
        replies_received=replies,
        interested=interested,
        meetings_booked=booked,
        cost_units_total=cost_units,
        cost_units_per_meeting=per_meeting,
        cost_usd_per_meeting=usd_per_meeting,
    )


# ── Phase 4: cost attribution per TENANT (client profitability view) ────────


@dataclass(frozen=True)
class TenantEconomicsReport:
    tenant_id: uuid.UUID
    leads_total: int
    leads_qualified: int
    sends_completed: int
    replies_received: int
    interested: int
    meetings_booked: int
    cost_units_total: float
    #: Rolling-30-day spend — the number the monthly cap governs.
    cost_units_30d: float
    cost_units_per_meeting: float | None
    cost_usd_per_meeting: float | None
    monthly_spend_cap_units: float | None

    @property
    def spend_cap_remaining_units(self) -> float | None:
        if self.monthly_spend_cap_units is None:
            return None
        return max(0.0, self.monthly_spend_cap_units - self.cost_units_30d)

    @property
    def funnel(self) -> dict[str, int]:
        return {
            "leads": self.leads_total,
            "qualified": self.leads_qualified,
            "sent": self.sends_completed,
            "replied": self.replies_received,
            "interested": self.interested,
            "booked": self.meetings_booked,
        }


def _count_reached_tenant(session, state: str) -> int:
    """Leads of this tenant (RLS-scoped session) that ever reached
    ``state``, whatever happened to them afterwards."""
    return session.execute(
        select(func.count(distinct(LeadTransition.lead_id))).where(
            LeadTransition.to_state == state
        )
    ).scalar_one()


def tenant_economics(tenant_id: uuid.UUID) -> TenantEconomicsReport:
    """Cross-campaign cost attribution for one tenant — cost per booked
    meeting and spend against the tenant's monthly cap, derived from rows
    the pipeline already writes."""
    settings = get_settings()
    cutoff = datetime.now(tz=UTC) - timedelta(days=30)
    with tenant_session(tenant_id) as session:
        leads_total = session.execute(
            select(func.count()).select_from(Lead)
        ).scalar_one()
        qualified = _count_reached_tenant(session, "scored_qualified")
        interested = _count_reached_tenant(session, "interested")
        booked = _count_reached_tenant(session, "booked")
        sends = session.execute(
            select(func.count()).where(SendJob.status == "sent")
        ).scalar_one()
        replies = session.execute(select(func.count()).select_from(Reply)).scalar_one()
        cost_total = float(
            session.execute(
                select(func.coalesce(func.sum(PipelineRun.cost_units), 0))
            ).scalar_one()
        )
        cost_30d = float(
            session.execute(
                select(func.coalesce(func.sum(PipelineRun.cost_units), 0)).where(
                    PipelineRun.started_at >= cutoff
                )
            ).scalar_one()
        )
        tenant = session.get(Tenant, tenant_id)
        cap = (
            float(tenant.monthly_spend_cap_units)
            if tenant is not None and tenant.monthly_spend_cap_units is not None
            else None
        )

    per_meeting = (cost_total / booked) if booked else None
    usd_rate = settings.cost_unit_usd
    usd_per_meeting = (
        per_meeting * usd_rate if (per_meeting is not None and usd_rate > 0) else None
    )
    return TenantEconomicsReport(
        tenant_id=tenant_id,
        leads_total=leads_total,
        leads_qualified=qualified,
        sends_completed=sends,
        replies_received=replies,
        interested=interested,
        meetings_booked=booked,
        cost_units_total=cost_total,
        cost_units_30d=cost_30d,
        cost_units_per_meeting=per_meeting,
        cost_usd_per_meeting=usd_per_meeting,
        monthly_spend_cap_units=cap,
    )
