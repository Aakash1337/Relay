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

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from relay.config import get_settings
from relay.db.models import Campaign, Lead, OutreachDraft, SendJob
from relay.domain.suppression import is_suppressed
from relay.logs import get_logger

log = get_logger(__name__)

#: Lawful bases acceptable for a *simulated* (synthetic/seed) send.
_SIMULATED_SAFE_BASES = frozenset(
    {
        "synthetic",
        "test_consent",
        "consent",
        "contract",
        "legitimate_interest",
        "client_warranty",
    }
)


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
) -> EligibilityResult:
    """Run the full §10 checklist for one prospective send."""
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
    check(
        "lawful_send_basis",
        lead.lawful_basis in _SIMULATED_SAFE_BASES,
        f"lawful_basis={lead.lawful_basis}, region={lead.region_assumption} "
        "(region-specific rules land with the Legal/Data Preflight, "
        "Phase 1B)",
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
    duplicate = session.execute(
        select(SendJob.id).where(
            SendJob.tenant_id == lead.tenant_id,
            SendJob.campaign_id == lead.campaign_id,
            SendJob.lead_id == lead.id,
            SendJob.sequence_step == 1,
            SendJob.message_version == draft.version,
        )
    ).first()
    check(
        "idempotency_key_unused",
        duplicate is None,
        "duplicate send job exists" if duplicate else "unused",
    )

    # ── Real-infrastructure checks: hard failures for real mode ────────────
    if mode == "real":
        settings = get_settings()
        check(
            "real_send_enabled",
            settings.real_send_enabled,
            "RELAY_REAL_SEND_ENABLED is false",
        )
        check(
            "sender_identity_approved",
            False,
            "no approved sender identity exists (Phase 1C)",
        )
        check(
            "domain_authenticated",
            False,
            "SPF/DKIM/DMARC not configured (Phase 1C/3 deliverability)",
        )
        check(
            "mailbox_active_below_cap",
            False,
            "no mailbox infrastructure exists (Phase 1C)",
        )
        check(
            "campaign_below_thresholds",
            False,
            "complaint/bounce threshold policies land in Phase 3",
        )
        check(
            "unsubscribe_mechanism_present",
            False,
            "unsubscribe headers require a sending provider (Phase 1C)",
        )
        check(
            "provider_terms_allow",
            False,
            "Sending Provider Decision Record not completed (§6)",
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
