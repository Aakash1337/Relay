"""Phase 2 adversarial/correctness suite (roadmap list, made executable).

Each test attacks an invariant the way a bug, a race, or an adversary
would — concurrency, replays, conflicting writes, raw SQL — and asserts
the structural layer holds.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from relay.config import get_settings
from relay.crm.base import CRMLeadSnapshot
from relay.crm.memory import InMemoryCRM
from relay.db.engine import tenant_session
from relay.db.models import (
    Lead,
    LeadTransition,
    Reply,
    SendJob,
    Suppression,
)
from relay.domain import dsr, eligibility
from relay.domain.suppression import add_suppression
from relay.hashing import hash_email
from relay.pipeline.runner import PipelineRunner
from relay.synthetic.generator import ReplyIntent
from relay.synthetic.seed import create_simulated_reply
from relay.workers.send_worker import process_pending
from tests.conftest import (
    approve_current_draft,
    run_to_approval,
    walk_to_closed,
    walk_to_sent,
)

pytestmark = pytest.mark.exit_gate


def _queue_lead(tenant_id, factory) -> uuid.UUID:
    lead_id = factory.lead()
    run_to_approval(tenant_id, lead_id)
    approve_current_draft(tenant_id, lead_id)
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.stopped_on == "waiting_worker"
    return lead_id


# ── Duplicate-send chaos: concurrent workers race one queue ─────────────────


def test_duplicate_send_chaos_concurrent_workers(tenant_a, factory_a):
    """Four workers race the same queue. FOR UPDATE SKIP LOCKED plus the
    idempotency UNIQUE mean exactly one send per job, no matter who wins."""
    tenant_id, _ = tenant_a
    lead_ids = [_queue_lead(tenant_id, factory_a) for _ in range(3)]

    with ThreadPoolExecutor(max_workers=4) as pool:
        stats = list(pool.map(lambda _: process_pending(max_jobs=10), range(4)))

    assert sum(s.sent for s in stats) == 3  # one send per job, total
    with tenant_session(tenant_id) as session:
        jobs = session.execute(select(SendJob)).scalars().all()
        assert sorted(j.status for j in jobs) == ["sent", "sent", "sent"]
        for lead_id in lead_ids:
            transitions = (
                session.execute(
                    select(LeadTransition).where(
                        LeadTransition.lead_id == lead_id,
                        LeadTransition.to_state == "sent",
                    )
                )
                .scalars()
                .all()
            )
            assert len(transitions) == 1  # never double-sent


# ── Transactional outbox: transition and job commit or roll back together ──


def _dup_insert(session, src_job_id, *, key_suffix: str) -> None:
    """Attempt to clone a send job's row (optionally with a mutated
    idempotency key) — the way a replayed/raced enqueue would."""
    session.execute(
        text(
            "INSERT INTO send_jobs (tenant_id, campaign_id, lead_id,"
            " draft_id, sequence_step, message_version, idempotency_key,"
            " mode, recipient_email_hash, recipient_domain) SELECT"
            " tenant_id, campaign_id, lead_id, draft_id, sequence_step,"
            " message_version, idempotency_key || :suffix, mode,"
            " recipient_email_hash, recipient_domain FROM send_jobs"
            " WHERE id = :id"
        ),
        {"id": str(src_job_id), "suffix": key_suffix},
    )


def test_duplicate_send_job_blocked_by_both_unique_constraints(tenant_a, factory_a):
    """The outbox is guarded by TWO independent unique constraints, and a
    raced/replayed enqueue must hit one or the other, never slip a second
    job through:

    - same idempotency_key  -> uq_send_jobs_idempotency_key
    - same natural key (tenant,campaign,lead,step,version), different
      idempotency_key -> uq_send_jobs_idempotency

    Either way the datastore stays coherent: exactly one job for the lead.
    """
    tenant_id, _ = tenant_a
    lead_id = _queue_lead(tenant_id, factory_a)
    with tenant_session(tenant_id) as session:
        job_id = session.execute(select(SendJob.id)).scalar_one()

    # 1. Exact idempotency-key collision.
    with pytest.raises(IntegrityError, match="idempotency_key"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            _dup_insert(session, job_id, key_suffix="")

    # 2. Different idempotency key, same natural key -> the OTHER constraint.
    with pytest.raises(IntegrityError):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            _dup_insert(session, job_id, key_suffix="-x")

    with tenant_session(tenant_id) as session:
        jobs = session.execute(
            select(SendJob).where(SendJob.lead_id == lead_id)
        ).scalars()
        assert len(jobs.all()) == 1


# ── Suppression bypass: every route to a suppressed send is closed ─────────


def test_suppression_bypass_all_paths_closed(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = _queue_lead(tenant_id, factory_a)  # queued BEFORE suppression

    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        # (Suppression rows are INSERT-only for the app role — pointing an
        # existing entry at a different address via UPDATE is itself
        # impossible, which is its own layer of this defense.)
        add_suppression(
            session,
            tenant_id=tenant_id,
            reason="manual",
            source="manual",
            created_by="adversary-test",
            scope="tenant",
            email=lead.email,
        )

    # Path 1: the worker's execution-time re-check blocks the queued job.
    stats = process_pending()
    assert stats.sent == 0 and stats.blocked == 1

    # Path 2: eligibility (code layer) says no for any future attempt.
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead.state == "send_blocked"

    # Path 3: raw SQL re-queue attempt — the claim trigger re-checks
    # suppression structurally. (A fresh INSERT is blocked by the
    # one-active partial index + trigger; flipping the blocked job back
    # is blocked by the status machine.)
    with pytest.raises(IntegrityError):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.execute(
                text("UPDATE send_jobs SET status = 'queued' WHERE tenant_id = :t"),
                {"t": str(tenant_id)},
            )


# ── Webhook replay: a replayed reply cannot double-transition ───────────────


def test_replayed_reply_webhook_cannot_double_transition(tenant_a, factory_a):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    walk_to_closed(tenant_id, lead_id)

    with tenant_session(tenant_id) as session:
        transitions_before = session.execute(
            select(LeadTransition).where(LeadTransition.lead_id == lead_id)
        ).scalars()
        count_before = len(transitions_before.all())

    # The replay: the same reply arrives again (provider redelivery).
    create_simulated_reply(tenant_id, lead_id, intent=ReplyIntent.INTERESTED)
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.final_state == "closed"  # terminal stays terminal

    with tenant_session(tenant_id) as session:
        count_after = len(
            session.execute(
                select(LeadTransition).where(LeadTransition.lead_id == lead_id)
            )
            .scalars()
            .all()
        )
        assert count_after == count_before  # zero new transitions
        # The replayed reply exists as data (evidence), untriaged forever.
        replies = session.execute(
            select(Reply).where(Reply.lead_id == lead_id)
        ).scalars()
        assert len(replies.all()) == 2


def test_replayed_unsubscribe_does_not_duplicate_suppression_effect(
    tenant_a, factory_a
):
    tenant_id, _ = tenant_a
    lead_id = factory_a.lead()
    walk_to_sent(tenant_id, lead_id)
    create_simulated_reply(tenant_id, lead_id, intent=ReplyIntent.UNSUBSCRIBE)
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.final_state == "unsubscribed"

    # Replay the unsubscribe. Terminal lead: nothing moves, nothing fires.
    create_simulated_reply(tenant_id, lead_id, intent=ReplyIntent.UNSUBSCRIBE)
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.final_state == "unsubscribed"
    with tenant_session(tenant_id) as session:
        entries = session.execute(select(Suppression)).scalars().all()
        assert len(entries) == 1  # auto-suppress fired exactly once


# ── CRM conflict: the canonical datastore wins, deterministically ──────────


def test_crm_conflict_resolution_last_canonical_write_wins():
    crm = InMemoryCRM()
    ref = str(uuid.uuid4())
    stale = CRMLeadSnapshot(
        external_ref=ref,
        tenant_ref="t",
        email="x@example.test",
        state="approval_pending",
        first_name="Old",
    )
    fresh = CRMLeadSnapshot(
        external_ref=ref,
        tenant_ref="t",
        email="x@example.test",
        state="sent",
        first_name="New",
    )
    crm.upsert_lead(stale)
    # Conflict: someone edited the mirror out-of-band; RELAY re-syncs.
    crm.leads[ref] = CRMLeadSnapshot(
        external_ref=ref,
        tenant_ref="t",
        email="x@example.test",
        state="hand-edited-nonsense",
    )
    crm.upsert_lead(fresh)
    assert crm.leads[ref].state == "sent"  # canonical store won
    assert crm.leads[ref].first_name == "New"


# ── Cross-tenant erasure via API auth boundaries ────────────────────────────


def test_tenant_b_erasure_cannot_touch_tenant_a_data(tenant_a, tenant_b, factory_a):
    tenant_id, _ = tenant_a
    other, _ = tenant_b
    email = f"victim-{uuid.uuid4().hex[:6]}@example.test"
    lead_id = factory_a.lead(email=email)

    result = dsr.execute_erasure(other, email=email, requested_by="attacker")
    assert result.datastore["leads"] == 0  # nothing of A's was visible

    with tenant_session(tenant_id) as session:
        assert session.get(Lead, lead_id) is not None  # untouched
        # And A gained no suppression entry from B's request.
        assert (
            session.execute(
                select(Suppression).where(Suppression.email_hash == hash_email(email))
            )
            .scalars()
            .all()
            == []
        )


# ── PII redaction sweep: raw addresses never reach logs ────────────────────


def test_raw_email_never_appears_in_logs(tenant_a, factory_a, capsys):
    tenant_id, _ = tenant_a
    email = f"pii-sweep-{uuid.uuid4().hex[:6]}@example.test"
    lead_id = factory_a.lead(email=email)
    walk_to_closed(tenant_id, lead_id)
    dsr.execute_erasure(tenant_id, email=email, requested_by="dpo")

    captured = capsys.readouterr()
    assert email not in captured.err
    assert email not in captured.out
    # The hash (suppression-compatible identity) IS allowed to appear.


# ── Backup/restore: erasure survives a restore ──────────────────────────────


def test_backup_restore_preserves_erasure(tenant_a, factory_a):
    """Phase 2 exit gate: restore-from-backup is tested — and specifically
    that a DSR erasure is not silently undone by restoring a newer dump."""
    if not (shutil.which("pg_dump") and shutil.which("psql")):
        pytest.skip("pg_dump/psql not available")

    tenant_id, _ = tenant_a
    email = f"restore-{uuid.uuid4().hex[:6]}@example.test"
    lead_id = factory_a.lead(email=email)
    walk_to_sent(tenant_id, lead_id)
    dsr.execute_erasure(tenant_id, email=email, requested_by="dpo")

    settings = get_settings()
    # postgresql+psycopg://user:pass@host:port/db → libpq URL
    url = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
    base, _, dbname = url.rpartition("/")
    restore_db = f"{dbname}_restore"
    env = dict(os.environ)

    def run(cmd: list[str]) -> None:
        subprocess.run(cmd, check=True, capture_output=True, env=env)

    run(["psql", url, "-c", f'DROP DATABASE IF EXISTS "{restore_db}"'])
    run(["psql", url, "-c", f'CREATE DATABASE "{restore_db}"'])
    dump = subprocess.run(
        ["pg_dump", "--no-owner", "--no-privileges", url],
        check=True,
        capture_output=True,
        env=env,
    )
    subprocess.run(
        ["psql", f"{base}/{restore_db}"],
        input=dump.stdout,
        check=True,
        capture_output=True,
        env=env,
    )

    import psycopg

    with psycopg.connect(f"{base}/{restore_db}") as conn:
        leads = conn.execute(
            "SELECT count(*) FROM leads WHERE email_hash = %s",
            (hash_email(email),),
        ).fetchone()[0]
        suppressed = conn.execute(
            "SELECT count(*) FROM suppression WHERE email_hash = %s",
            (hash_email(email),),
        ).fetchone()[0]
    run(["psql", url, "-c", f'DROP DATABASE IF EXISTS "{restore_db}"'])

    assert leads == 0  # the erased person is absent from the backup line
    assert suppressed == 1  # and the do-not-contact memory survived


# ── Eligibility layers agree under attack ───────────────────────────────────


def test_eligibility_code_and_trigger_agree_on_suppression(tenant_a, factory_a):
    """The code gate and the DB trigger must never drift on suppression.
    Queue a job FIRST, then suppress, then probe both layers on the SAME
    suppressed lead:

    - trigger layer: a raw claim (queued -> sending) is rejected by
      fn_send_jobs_guard's claim-time suppression re-check;
    - code layer: the worker's execution-time eligibility gate blocks it.
    """
    tenant_id, _ = tenant_a
    lead_id = _queue_lead(tenant_id, factory_a)  # job 'queued', lead send_queued

    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        job_id = session.execute(select(SendJob.id)).scalar_one()
        add_suppression(
            session,
            tenant_id=tenant_id,
            reason="manual",
            source="manual",
            created_by="drift-test",
            email=lead.email,
        )

    # Trigger layer: raw claim of the queued job re-checks suppression.
    with pytest.raises(IntegrityError, match="suppress"):  # noqa: SIM117
        with tenant_session(tenant_id) as session:
            session.execute(
                text("UPDATE send_jobs SET status = 'sending' WHERE id = :id"),
                {"id": str(job_id)},
            )

    # Code layer: the worker's own gate blocks and parks the lead.
    stats = process_pending()
    assert stats.sent == 0 and stats.blocked == 1
    with tenant_session(tenant_id) as session:
        lead = session.get(Lead, lead_id)
        assert lead is not None and lead.state == "send_blocked"


def test_eligibility_exclusion_only_ignores_own_job(tenant_a, factory_a):
    """exclude_send_job_id must not become a loophole: excluding job X
    still counts job Y as a duplicate."""
    tenant_id, _ = tenant_a
    lead_id = _queue_lead(tenant_id, factory_a)
    with tenant_session(tenant_id) as session:
        job = session.execute(select(SendJob)).scalar_one()
        lead = session.get(Lead, lead_id)
        from relay.db.models import Campaign, OutreachDraft

        campaign = session.get(Campaign, job.campaign_id)
        draft = session.get(OutreachDraft, job.draft_id)
        result = eligibility.evaluate(
            session,
            lead=lead,
            campaign=campaign,
            draft=draft,
            mode="simulated",
            exclude_send_job_id=uuid.uuid4(),  # excludes NOTHING real
        )
        assert not result.eligible
        assert any(c.name == "idempotency_key_unused" for c in result.failures)
