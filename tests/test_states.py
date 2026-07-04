"""State machine definition tests: the Python map and the DB rule seed
must be the same machine."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from relay.db.engine import admin_engine
from relay.domain.states import (
    ALLOWED_TRANSITIONS,
    HAPPY_PATH,
    TERMINAL_STATES,
    LeadState,
    is_transition_allowed,
    transition_rule_rows,
)


def test_happy_path_is_legal_end_to_end():
    for src, dst in zip(HAPPY_PATH, HAPPY_PATH[1:], strict=False):
        assert is_transition_allowed(src, dst), f"{src} -> {dst}"


def test_happy_path_covers_first_and_last_states():
    assert HAPPY_PATH[0] is LeadState.CREATED
    assert HAPPY_PATH[-1] is LeadState.CLOSED


def test_terminal_states_have_no_outgoing_edges():
    for state in TERMINAL_STATES:
        assert state not in ALLOWED_TRANSITIONS or not ALLOWED_TRANSITIONS[state], (
            f"terminal state {state} has outgoing edges"
        )


def test_unsubscribed_is_terminal():
    assert LeadState.UNSUBSCRIBED in TERMINAL_STATES


def test_error_states_reachable_from_every_active_state():
    for state, targets in ALLOWED_TRANSITIONS.items():
        if state is LeadState.ERROR_RETRYABLE:
            continue
        assert LeadState.ERROR_TERMINAL in targets, state
        assert LeadState.ERROR_RETRYABLE in targets, state


def test_sent_only_reachable_from_send_queued():
    sources = [
        src for src, targets in ALLOWED_TRANSITIONS.items() if LeadState.SENT in targets
    ]
    # send_queued (the outbox path) and error_retryable (resume) only.
    assert set(sources) == {LeadState.SEND_QUEUED, LeadState.ERROR_RETRYABLE}


def test_db_rules_exactly_match_python_map():
    with admin_engine().connect() as conn:
        db_rows = set(
            conn.execute(
                text("SELECT from_state, to_state FROM lead_transition_rules")
            ).all()
        )
    assert db_rows == set(transition_rule_rows())


@pytest.mark.parametrize(
    ("src", "dst"),
    [
        (LeadState.CREATED, LeadState.SENT),
        (LeadState.CREATED, LeadState.CLOSED),
        (LeadState.APPROVAL_PENDING, LeadState.SEND_QUEUED),
        (LeadState.UNSUBSCRIBED, LeadState.SEND_QUEUED),
        (LeadState.CLOSED, LeadState.CREATED),
        (LeadState.SENT, LeadState.SENT),
    ],
)
def test_forbidden_shortcuts(src: LeadState, dst: LeadState):
    assert not is_transition_allowed(src, dst)
