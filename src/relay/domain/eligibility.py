"""The Send-Eligibility Gate (§10).

A message can send only if *every* check passes. Checked in code here,
and re-checked structurally by DB triggers immediately before execution.
Approval alone does not send: the human gate answers "is this content
right?", this gate answers "is this send lawful, suppression-clear,
authenticated, and non-duplicate?".

Phase 0 posture: the checks that require real infrastructure
(deliverability, provider terms, sender identity) are implemented as
*hard failures for real mode* — not permissive stubs. A real send is
structurally ineligible until those phases land. Simulated sends skip
only the checks that are meaningless without real infrastructure; the
integrity checks (suppression, verification, approval, idempotency,
tenant match) always apply.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from relay.config import get_settings
from relay.db.models import Campaign, Lead, OutreachDraft, SendJob, Suppression
from relay.domain.suppression import is_suppressed
from relay.domain.vocab import (
    REAL_DATA_BASES,
    SIMULATED_SAFE_BASES,
    LawfulBasis,
)
from relay.hashing import hash_email
from relay.logs import get_logger
from relay.senders import real_sender_status

log = get_logger(__name__)


@dataclass(frozen=True)
class EligibilityCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class EligibilityResult:
    checks: tuple[EligibilityCheck, ...]

    @property
    def eligible(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failures(self) -> tuple[EligibilityCheck, ...]:
        return tuple(c for c in self.checks if not c.passed)

    def failure_summary(self) -> str:
        return "; ".join(f"{c.name}: {c.detail}" for c in self.failures)


def evaluate(
    session: Session,
    *,
    lead: Lead,
    campaign: Campaign,
    draft: OutreachDraft,
    mode: str,
    exclude_send_job_id: uuid.UUID | None = None,
) -> EligibilityResult:
    """Run the full §10 checklist for one prospective send.

    ``exclude_send_job_id`` is the job currently being executed: at
    execution time the job itself IS the idempotency record, so it must not
    count as a duplicate of itself. The worker passes its claimed job id
    here rather than post-filtering the result by check name.
    """
    checks: list[EligibilityCheck] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append(EligibilityCheck(name, bool(passed), detail))

    # ── Integrity checks: apply in every mode ───────────────────────────────
    suppressed = is_suppressed(
        session,
        tenant_id=lead.tenant_id,
        email_hash=lead.email_hash,
        domain=lead.email_domain,
        campaign_id=lead.campaign_id,
        mailbox_id=campaign.mailbox_id,
    )
    check(
        "not_suppressed",
        not suppressed,
        "recipient is on a suppression list" if suppressed else "clear",
    )
    check(
        "email_verified",
        lead.email_verified,
        "verified" if lead.email_verified else "email not verified",
    )
    # NOTE: SIMULATED_SAFE_BASES == every valid basis today, so this check
    # cannot fail for a DB-valid basis — it is a placeholder for the
    # region-specific lawful-basis rules that the Legal/Data Preflight will
    # populate (region_assumption is stored on every lead for exactly that
    # future use, but no gate reads it yet). Kept as a named seam.
    check(
        "lawful_send_basis",
        lead.lawful_basis in SIMULATED_SAFE_BASES,
        f"lawful_basis={lead.lawful_basis}, region={lead.region_assumption} "
        "(region-specific rules are a future Legal/Data Preflight item)",
    )
    # Phase 1B invariant: the four real-DATA bases are draft-only in every
    # mode. Phase 1C opened real sends ONLY for test_consent (a
    # compliance-free basis, our own inboxes) — the real-data bases remain
    # fully blocked here and in fn_send_jobs_guard. Real-prospect sending
    # is gated behind the §6 production-provider revisit criteria.
    real_person = LawfulBasis(lead.lawful_basis) in REAL_DATA_BASES
    check(
        "send_path_open_for_basis",
        not real_person,
        "real-data leads stop at draft (sending gated to §6 production work)"
        if real_person
        else "compliance-free basis",
    )
    check(
        "approved_draft_current_version",
        draft.status == "approved" and lead.approved_message_version == draft.version,
        f"draft status={draft.status}, draft version={draft.version}, "
        f"approved version={lead.approved_message_version}",
    )
    check(
        "tenant_mailbox_match",
        lead.tenant_id == campaign.tenant_id == draft.tenant_id
        and lead.campaign_id == campaign.id
        and draft.lead_id == lead.id,
        "lead, campaign, and draft belong to the same tenant and chain",
    )
    duplicate_query = select(SendJob.id).where(
        SendJob.tenant_id == lead.tenant_id,
        SendJob.campaign_id == lead.campaign_id,
        SendJob.lead_id == lead.id,
        SendJob.sequence_step == 1,
        SendJob.message_version == draft.version,
    )
    if exclude_send_job_id is not None:
        duplicate_query = duplicate_query.where(SendJob.id != exclude_send_job_id)
    duplicate = session.execute(duplicate_query).first()
    check(
        "idempotency_key_unused",
        duplicate is None,
        "duplicate send job exists" if duplicate else "unused",
    )

    # ── Real-infrastructure checks (Phase 1C: config attests + live data) ──
    # Each attest is a recorded human claim in deployment config, not a
    # guess by code; the caps are computed live from the datastore.
    if mode == "real":
        settings = get_settings()
        check(
            "real_send_enabled",
            settings.real_send_enabled,
            "RELAY_REAL_SEND_ENABLED is false",
        )
        # §6 pilot rule: real sends go ONLY to inboxes whose owners
        # explicitly consented to testing (our own). Re-checked by the
        # DB trigger. Opens beyond test_consent only with the production
        # provider work in the §6 revisit criteria.
        check(
            "real_mode_basis_is_test_consent",
            lead.lawful_basis == str(LawfulBasis.TEST_CONSENT),
            f"1C pilot sends only to test_consent inboxes "
            f"(lawful_basis={lead.lawful_basis})",
        )
        # §6 pilot allowlist: a REAL send may target ONLY an address on
        # RELAY_PILOT_RECIPIENTS (our own verified inboxes). This is an
        # explicit structural gate on top of test_consent + the SES sandbox,
        # fail-closed: an empty allowlist means no real send is possible.
        # Re-checked at the last hop by the sender.
        allowlist = {hash_email(addr) for addr in settings.pilot_recipient_addresses()}
        check(
            "recipient_on_pilot_allowlist",
            lead.email_hash in allowlist,
            "recipient not on RELAY_PILOT_RECIPIENTS allowlist"
            if allowlist
            else "RELAY_PILOT_RECIPIENTS is empty (no real send allowed)",
        )
        # Provider-neutral: is a real sender actually configured and
        # constructible (provider set + its required settings present)?
        # Reads no provider-specific setting, so swapping SES for another
        # direct provider never touches this gate.
        sender_ok, sender_reason = real_sender_status()
        check("sender_configured", sender_ok, sender_reason)
        # The human attests remain separate from "is it wired up".
        check(
            "sender_identity_approved",
            settings.sender_identity_approved,
            "RELAY_SENDER_IDENTITY_APPROVED attest missing",
        )
        check(
            "domain_authenticated",
            settings.sender_domain_authenticated,
            "SPF/DKIM/DMARC attest missing (RELAY_SENDER_DOMAIN_AUTHENTICATED)",
        )
        recent_real_sends = session.execute(
            select(func.count()).where(
                SendJob.tenant_id == lead.tenant_id,
                SendJob.mode == "real",
                SendJob.status.in_(("sending", "sent")),
                SendJob.queued_at >= datetime.now(tz=UTC) - timedelta(hours=24),
            )
        ).scalar_one()
        check(
            "mailbox_active_below_cap",
            recent_real_sends < settings.real_send_daily_cap,
            f"{recent_real_sends} real sends in 24h "
            f"(cap {settings.real_send_daily_cap})",
        )
        window_start = datetime.now(tz=UTC) - timedelta(
            days=settings.bounce_complaint_window_days
        )
        recent_bounces = session.execute(
            select(func.count()).where(
                Suppression.tenant_id == lead.tenant_id,
                Suppression.reason.in_(("hard_bounce", "complaint")),
                Suppression.created_at >= window_start,
            )
        ).scalar_one()
        check(
            "campaign_below_thresholds",
            recent_bounces < settings.max_bounces_complaints_in_window,
            f"{recent_bounces} bounces/complaints in "
            f"{settings.bounce_complaint_window_days}d window "
            f"(max {settings.max_bounces_complaints_in_window})",
        )
        check(
            "unsubscribe_mechanism_present",
            bool(settings.unsubscribe_mailto),
            "RELAY_UNSUBSCRIBE_MAILTO not set (List-Unsubscribe header)",
        )
        check(
            "provider_terms_allow",
            bool(settings.provider_terms_record),
            "RELAY_PROVIDER_TERMS_RECORD must reference the §6 decision "
            "record authorizing this provider",
        )

    result = EligibilityResult(tuple(checks))
    log.info(
        "send eligibility evaluated",
        lead_id=str(lead.id),
        mode=mode,
        eligible=result.eligible,
        failures=[c.name for c in result.failures],
    )
    return result
