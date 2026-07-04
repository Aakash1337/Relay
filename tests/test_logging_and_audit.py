"""Observability foundations: PII redaction in logs and audit payloads;
append-only audit trail."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from relay import audit
from relay.db.engine import admin_session, tenant_session
from relay.db.models import AuditLog
from relay.hashing import hash_email
from relay.logs import get_logger, redact_payload
from tests.conftest import walk_to_closed

pytestmark = pytest.mark.exit_gate


# ── Redaction unit behavior ─────────────────────────────────────────────────


def test_emails_inside_strings_are_hashed():
    payload = redact_payload(
        {"note": "please contact Jane.Doe@Example.COM about pricing"}
    )
    assert "Jane.Doe" not in json.dumps(payload)
    assert hash_email("jane.doe@example.com")[:12] in payload["note"]


def test_denylisted_keys_are_dropped_entirely():
    payload = redact_payload(
        {
            "api_key": "rk_secret",
            "password": "hunter2",
            "first_name": "Jane",
            "last_name": "Doe",
            "email": "jane@example.com",
            "body": "the whole outreach message",
            "safe_field": "stays",
        }
    )
    dumped = json.dumps(payload)
    for secret in ("rk_secret", "hunter2", "Jane", "Doe", "jane@", "whole"):
        assert secret not in dumped
    assert payload["safe_field"] == "stays"
    assert payload["password"] == "[REDACTED]"


def test_redaction_recurses_into_nested_structures():
    payload = redact_payload(
        {
            "outer": {
                "token": "abc123",
                "list": ["reach me at foo@bar.test", {"secret": "x"}],
            }
        }
    )
    dumped = json.dumps(payload)
    assert "abc123" not in dumped
    assert "foo@bar.test" not in dumped


# ── Redaction wired into the real log pipeline ─────────────────────────────


def test_log_lines_never_contain_raw_emails(capsys):
    log = get_logger("test")
    log.info(
        "lead touched",
        detail="wrote to Person.Name@Company.COM today",
        email="person.name@company.com",
    )
    err = capsys.readouterr().err
    assert "Person.Name@Company.COM" not in err
    assert "person.name@company.com" not in err
    line = json.loads([ln for ln in err.splitlines() if ln.startswith("{")][-1])
    assert line["email"] == "[REDACTED]"
    assert hash_email("person.name@company.com")[:12] in line["detail"]


def test_full_journey_logs_are_email_free(tenant_a, factory_a, capsys):
    """The whole exit-gate journey, and not one raw address in the log."""
    tenant_id, _ = tenant_a
    email = "traceable-prospect@example.test"
    lead_id = factory_a.lead(email=email)
    walk_to_closed(tenant_id, lead_id)
    err = capsys.readouterr().err
    assert email not in err
    assert "traceable-prospect" not in err


# ── Audit payloads are redacted at rest ─────────────────────────────────────


def test_audit_payload_is_redacted(tenant_a):
    tenant_id, _ = tenant_a
    with tenant_session(tenant_id) as session:
        audit.record(
            session,
            tenant_id=tenant_id,
            actor_type="system",
            actor_id="test",
            action="test.action",
            payload={
                "email": "audited@example.test",
                "note": "call audited@example.test tomorrow",
            },
        )
    with tenant_session(tenant_id) as session:
        entry = session.execute(select(AuditLog)).scalar_one()
        dumped = json.dumps(entry.payload)
        assert "audited@example.test" not in dumped


# ── Append-only audit trail ─────────────────────────────────────────────────


def test_audit_log_rejects_update_and_delete(tenant_a):
    tenant_id, _ = tenant_a
    with tenant_session(tenant_id) as session:
        audit.record(
            session,
            tenant_id=tenant_id,
            actor_type="system",
            actor_id="test",
            action="immutable.check",
        )

    # The app role lacks UPDATE/DELETE grants entirely.
    with pytest.raises(ProgrammingError):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.execute(text("UPDATE audit_log SET action = 'tampered'"))
    # And even the schema owner is stopped by the trigger.
    with pytest.raises(IntegrityError, match="append-only"):  # noqa: SIM117
        with admin_session() as session:
            session.execute(text("UPDATE audit_log SET action = 'tampered'"))
    with pytest.raises(IntegrityError, match="append-only"):  # noqa: SIM117
        with admin_session() as session:
            session.execute(text("DELETE FROM audit_log"))


def test_approval_and_send_are_audited(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    walk_to_closed(tenant_id, lead_id)
    with tenant_session(tenant_id) as session:
        actions = set(session.execute(select(AuditLog.action)).scalars().all())
    assert "draft.approve" in actions
    assert "send.executed" in actions
    assert "lead.transition" in actions
