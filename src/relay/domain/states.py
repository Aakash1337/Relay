"""The lead state machine (project documentation §4) — single source of truth.

This module defines every state and every legal transition. The same map:

- gates transitions in code (relay.domain.state_machine), and
- seeds the ``lead_transition_rules`` table at migration time, which the
  ``fn_enforce_lead_transition`` DB trigger checks on every UPDATE.

So the machine is enforced in code AND in database constraints — the
planner (or any buggy caller) cannot invent a transition.

Deviations from the doc, made explicit:

- ``(any) → error_*`` is encoded as *(any active state)* → error_*.
  Terminal states have no outgoing edges at all — "unsubscribed is
  terminal" would be violated by unsubscribed → error_retryable → resume.
- ``error_retryable → <active state>`` is allowed by the DB rule set; code
  additionally narrows the resume target to the recorded
  ``error_return_state``. The retry cap is enforced by the DB trigger.
- ``send_queued → send_blocked`` is added: the internal send worker
  re-checks every invariant at execution time (doc §10) and needs a legal
  landing state when an execution-time check fails after queueing.
"""

from __future__ import annotations

from enum import StrEnum


class LeadState(StrEnum):
    CREATED = "created"
    SOURCE_CHECKED = "source_checked"
    SOURCE_REJECTED = "source_rejected"
    ENRICHMENT_PENDING = "enrichment_pending"
    ENRICHED = "enriched"
    VERIFICATION_PENDING = "verification_pending"
    VERIFICATION_FAILED = "verification_failed"
    VERIFIED = "verified"
    SCORING_PENDING = "scoring_pending"
    SCORED_REJECTED = "scored_rejected"
    SCORED_QUALIFIED = "scored_qualified"
    SHORTLIST_PENDING = "shortlist_pending"
    SHORTLIST_SKIPPED = "shortlist_skipped"
    PERSONALIZATION_PENDING = "personalization_pending"
    DRAFT_READY = "draft_ready"
    APPROVAL_PENDING = "approval_pending"
    REJECTED_BY_HUMAN = "rejected_by_human"
    APPROVED = "approved"
    SEND_ELIGIBILITY_PENDING = "send_eligibility_pending"
    SEND_BLOCKED = "send_blocked"
    SEND_QUEUED = "send_queued"
    SENT = "sent"
    BOUNCE_RECEIVED = "bounce_received"
    REPLY_RECEIVED = "reply_received"
    TRIAGE_PENDING = "triage_pending"
    UNSUBSCRIBED = "unsubscribed"
    NOT_INTERESTED = "not_interested"
    INTERESTED = "interested"
    BOOKING_PENDING = "booking_pending"
    BOOKED = "booked"
    CLOSED = "closed"
    ERROR_RETRYABLE = "error_retryable"
    ERROR_TERMINAL = "error_terminal"


#: States with no outgoing edges. A lead here is done, one way or another.
TERMINAL_STATES: frozenset[LeadState] = frozenset(
    {
        LeadState.SOURCE_REJECTED,
        LeadState.VERIFICATION_FAILED,
        LeadState.SCORED_REJECTED,
        LeadState.SHORTLIST_SKIPPED,
        LeadState.REJECTED_BY_HUMAN,
        LeadState.SEND_BLOCKED,
        LeadState.BOUNCE_RECEIVED,
        LeadState.UNSUBSCRIBED,
        LeadState.NOT_INTERESTED,
        LeadState.CLOSED,
        LeadState.ERROR_TERMINAL,
    }
)

#: States a lead can act from (everything except terminal + error_retryable).
ACTIVE_STATES: frozenset[LeadState] = frozenset(
    s
    for s in LeadState
    if s not in TERMINAL_STATES and s is not LeadState.ERROR_RETRYABLE
)

#: The forward pipeline edges from project documentation §4.
_PIPELINE_TRANSITIONS: dict[LeadState, frozenset[LeadState]] = {
    LeadState.CREATED: frozenset({LeadState.SOURCE_CHECKED}),
    LeadState.SOURCE_CHECKED: frozenset(
        {LeadState.SOURCE_REJECTED, LeadState.ENRICHMENT_PENDING}
    ),
    LeadState.ENRICHMENT_PENDING: frozenset({LeadState.ENRICHED}),
    LeadState.ENRICHED: frozenset({LeadState.VERIFICATION_PENDING}),
    LeadState.VERIFICATION_PENDING: frozenset(
        {LeadState.VERIFICATION_FAILED, LeadState.VERIFIED}
    ),
    LeadState.VERIFIED: frozenset({LeadState.SCORING_PENDING}),
    LeadState.SCORING_PENDING: frozenset(
        {LeadState.SCORED_REJECTED, LeadState.SCORED_QUALIFIED}
    ),
    # scored_qualified forks on the campaign's shortlist_required flag:
    # straight to drafting (default), or to the human shortlist first.
    LeadState.SCORED_QUALIFIED: frozenset(
        {LeadState.PERSONALIZATION_PENDING, LeadState.SHORTLIST_PENDING}
    ),
    # shortlist_pending is a WAIT state: a human pursues (→ drafting) or
    # skips (→ terminal). Skipped leads are never drafted or emailed.
    LeadState.SHORTLIST_PENDING: frozenset(
        {LeadState.PERSONALIZATION_PENDING, LeadState.SHORTLIST_SKIPPED}
    ),
    LeadState.PERSONALIZATION_PENDING: frozenset({LeadState.DRAFT_READY}),
    LeadState.DRAFT_READY: frozenset({LeadState.APPROVAL_PENDING}),
    LeadState.APPROVAL_PENDING: frozenset(
        {LeadState.REJECTED_BY_HUMAN, LeadState.APPROVED}
    ),
    LeadState.APPROVED: frozenset({LeadState.SEND_ELIGIBILITY_PENDING}),
    LeadState.SEND_ELIGIBILITY_PENDING: frozenset(
        {LeadState.SEND_BLOCKED, LeadState.SEND_QUEUED}
    ),
    # send_queued → send_blocked: execution-time eligibility failure
    LeadState.SEND_QUEUED: frozenset({LeadState.SENT, LeadState.SEND_BLOCKED}),
    # sent → unsubscribed: a one-click unsubscribe (RFC 8058) arrives
    # without a reply — no triage step ever happens for it.
    # sent → personalization_pending: the sequence advance (§17,
    # un-deferred) — no reply after the campaign's delay, more steps
    # remain, so the lead re-enters the drafting loop for step N+1.
    LeadState.SENT: frozenset(
        {
            LeadState.BOUNCE_RECEIVED,
            LeadState.REPLY_RECEIVED,
            LeadState.UNSUBSCRIBED,
            LeadState.PERSONALIZATION_PENDING,
        }
    ),
    LeadState.REPLY_RECEIVED: frozenset({LeadState.TRIAGE_PENDING}),
    LeadState.TRIAGE_PENDING: frozenset(
        {
            LeadState.UNSUBSCRIBED,
            LeadState.NOT_INTERESTED,
            LeadState.INTERESTED,
        }
    ),
    LeadState.INTERESTED: frozenset({LeadState.BOOKING_PENDING}),
    LeadState.BOOKING_PENDING: frozenset({LeadState.BOOKED}),
    LeadState.BOOKED: frozenset({LeadState.CLOSED}),
}


def _build_transitions() -> dict[LeadState, frozenset[LeadState]]:
    table: dict[LeadState, set[LeadState]] = {
        state: set(targets) for state, targets in _PIPELINE_TRANSITIONS.items()
    }
    # (any active) → {error_retryable | error_terminal}
    for state in ACTIVE_STATES:
        table.setdefault(state, set()).update(
            {LeadState.ERROR_RETRYABLE, LeadState.ERROR_TERMINAL}
        )
    # error_retryable → resume into any active state (code narrows this to
    # the recorded error_return_state; the DB trigger enforces the retry cap)
    table[LeadState.ERROR_RETRYABLE] = set(ACTIVE_STATES) | {LeadState.ERROR_TERMINAL}
    return {state: frozenset(targets) for state, targets in table.items()}


#: Every legal (from → to) edge. THE authority for code and DB alike.
ALLOWED_TRANSITIONS: dict[LeadState, frozenset[LeadState]] = _build_transitions()


def is_transition_allowed(from_state: LeadState, to_state: LeadState) -> bool:
    return to_state in ALLOWED_TRANSITIONS.get(from_state, frozenset())


def transition_rule_rows() -> list[tuple[str, str]]:
    """Flat (from, to) pairs used to seed ``lead_transition_rules``."""
    return sorted(
        (str(src), str(dst))
        for src, targets in ALLOWED_TRANSITIONS.items()
        for dst in targets
    )


#: The happy path a synthetic lead walks in the Phase 0 exit-gate test.
HAPPY_PATH: tuple[LeadState, ...] = (
    LeadState.CREATED,
    LeadState.SOURCE_CHECKED,
    LeadState.ENRICHMENT_PENDING,
    LeadState.ENRICHED,
    LeadState.VERIFICATION_PENDING,
    LeadState.VERIFIED,
    LeadState.SCORING_PENDING,
    LeadState.SCORED_QUALIFIED,
    LeadState.PERSONALIZATION_PENDING,
    LeadState.DRAFT_READY,
    LeadState.APPROVAL_PENDING,
    LeadState.APPROVED,
    LeadState.SEND_ELIGIBILITY_PENDING,
    LeadState.SEND_QUEUED,
    LeadState.SENT,
    LeadState.REPLY_RECEIVED,
    LeadState.TRIAGE_PENDING,
    LeadState.INTERESTED,
    LeadState.BOOKING_PENDING,
    LeadState.BOOKED,
    LeadState.CLOSED,
)
