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


def sql_in_list(values: type[StrEnum]) -> str:
    """Render an enum as a SQL IN-list literal, e.g. 'a', 'b', 'c'."""
    return ", ".join(f"'{v}'" for v in values)
