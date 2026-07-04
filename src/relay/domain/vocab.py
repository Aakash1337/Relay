"""Shared controlled vocabularies — defined once, enforced everywhere.

The lawful-basis set in particular appears in three places (the DB CHECK,
the API request schema, and the eligibility gate). A single source keeps
them from silently diverging — the failure the state machine already
avoids by seeding its rules from one Python map. When Phase 1B adds or
removes a basis, it changes here and all three sites move together.
"""

from __future__ import annotations

from enum import StrEnum


class LawfulBasis(StrEnum):
    SYNTHETIC = "synthetic"
    TEST_CONSENT = "test_consent"
    CONSENT = "consent"
    CONTRACT = "contract"
    LEGITIMATE_INTEREST = "legitimate_interest"
    CLIENT_WARRANTY = "client_warranty"


#: Bases acceptable for a simulated (synthetic/seed) send in Phase 0.
#: Real-region rules (which bases are valid where) land with the
#: Legal/Data Preflight in Phase 1B.
SIMULATED_SAFE_BASES: frozenset[LawfulBasis] = frozenset(LawfulBasis)

#: Bases that involve no real person's data — compliance-free testing.
#: Everything else asserts lawful processing of a REAL person and is
#: gated behind the tenant's approved Legal/Data Preflight (Phase 1B).
#: NOTE: fn_lead_insert_guard and fn_send_jobs_guard hardcode this pair
#: in SQL; a pin test (tests/test_phase1b_gate.py) keeps them in lockstep.
COMPLIANCE_FREE_BASES: frozenset[LawfulBasis] = frozenset(
    {LawfulBasis.SYNTHETIC, LawfulBasis.TEST_CONSENT}
)

REAL_DATA_BASES: frozenset[LawfulBasis] = frozenset(LawfulBasis) - COMPLIANCE_FREE_BASES


class ReviewDecision(StrEnum):
    """The three outcomes of the human approval rubric (Phase 1A)."""

    APPROVED = "approved"
    APPROVED_WITH_EDITS = "approved_with_edits"
    REJECTED = "rejected"


class ReviewReason(StrEnum):
    """Controlled rubric vocabulary for edit/reject reasons.

    A controlled set (not free text) so review outcomes are aggregable:
    'why do drafts get rejected' must be a GROUP BY, because that metric
    steers prompt iteration and the Phase 1A economics gate.
    """

    INACCURATE_CLAIM = "inaccurate_claim"
    WEAK_PERSONALIZATION = "weak_personalization"
    WRONG_PERSON = "wrong_person"
    TONE = "tone"
    TOO_LONG = "too_long"
    COMPLIANCE_RISK = "compliance_risk"
    SUSPECTED_INJECTION = "suspected_injection"
    OTHER = "other"


class TriageCategory(StrEnum):
    """Reply-triage outcomes; each maps to exactly one lead state."""

    INTERESTED = "interested"
    NOT_INTERESTED = "not_interested"
    UNSUBSCRIBED = "unsubscribed"


def sql_in_list(values: type[StrEnum]) -> str:
    """Render an enum as a SQL IN-list literal, e.g. 'a', 'b', 'c'."""
    return ", ".join(f"'{v}'" for v in values)
