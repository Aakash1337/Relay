"""Shared fixtures. Every test runs against a real PostgreSQL —
the structural guarantees under test (RLS, triggers, unique constraints)
do not exist in SQLite or in mocks."""

from __future__ import annotations

import os
import uuid

# Environment must be set before any relay import (settings are cached).
os.environ.setdefault(
    "RELAY_DATABASE_URL",
    "postgresql+psycopg://relay@127.0.0.1:5433/relay_test",
)
os.environ.setdefault(
    "RELAY_APP_DATABASE_URL",
    "postgresql+psycopg://relay_app:relay_app@127.0.0.1:5433/relay_test",
)
os.environ.setdefault("RELAY_ADMIN_TOKEN", "test-admin-token")

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from relay.db.engine import admin_engine, tenant_session  # noqa: E402
from relay.db.migrate import migrate  # noqa: E402
from relay.db.models import (  # noqa: E402
    Campaign,
    Lead,
    LeadSourceRegister,
    OutreachDraft,
    Tenant,
)
from relay.domain.approval import approve_draft  # noqa: E402
from relay.hashing import (  # noqa: E402
    email_domain,
    hash_api_key,
    hash_email,
)
from relay.logs import setup_logging  # noqa: E402
from relay.pipeline.runner import PipelineRunner  # noqa: E402
from relay.workers.send_worker import process_pending  # noqa: E402

_TABLES = (
    "audit_log",
    "send_jobs",
    "outreach_drafts",
    "suppression",
    "lead_transitions",
    "pipeline_runs",
    "leads",
    "campaigns",
    "lead_source_register",
    "tenants",
)


@pytest.fixture(scope="session", autouse=True)
def _database() -> None:
    # migrate(reset=True) runs DROP SCHEMA public CASCADE. If an ambient
    # RELAY_DATABASE_URL is exported (direnv, CI, a docker shell) pointing at
    # a real database, os.environ.setdefault yields to it — and the reset
    # would destroy that database. Refuse unless the target is clearly a
    # test database.
    from relay.config import get_settings

    settings = get_settings()
    for url in (settings.database_url, settings.app_database_url):
        db_name = url.rsplit("/", 1)[-1]
        if "test" not in db_name:
            raise RuntimeError(
                f"refusing to run destructive tests against non-test database "
                f"{db_name!r}; point RELAY_DATABASE_URL at a *_test database"
            )
    setup_logging()
    migrate(reset=True)


@pytest.fixture(autouse=True)
def _clean_tables(_database: None) -> None:
    yield
    with admin_engine().begin() as conn:
        conn.exec_driver_sql(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")


def _create_tenant(name: str) -> tuple[uuid.UUID, str]:
    from relay.db.engine import admin_session

    api_key = f"rk_test_{uuid.uuid4().hex}"
    with admin_session() as session:
        tenant = Tenant(name=name, api_key_hash=hash_api_key(api_key))
        session.add(tenant)
        session.flush()
        tenant_id = tenant.id
    return tenant_id, api_key


@pytest.fixture
def tenant_a() -> tuple[uuid.UUID, str]:
    return _create_tenant(f"tenant-a-{uuid.uuid4().hex[:8]}")


@pytest.fixture
def tenant_b() -> tuple[uuid.UUID, str]:
    return _create_tenant(f"tenant-b-{uuid.uuid4().hex[:8]}")


class LeadFactory:
    """Creates the register → campaign → lead chain for one tenant."""

    def __init__(self, tenant_id: uuid.UUID):
        self.tenant_id = tenant_id

    def source(self, *, terms: str = "yes") -> uuid.UUID:
        with tenant_session(self.tenant_id) as session:
            source = LeadSourceRegister(
                tenant_id=self.tenant_id,
                name=f"synthetic-{uuid.uuid4().hex[:8]}",
                source_type="synthetic",
                terms_allow_use=terms,
                proof_of_lawful_use="synthetic test data",
            )
            session.add(source)
            session.flush()
            return source.id

    def campaign(
        self,
        *,
        dry_run: bool = True,
        simulated_replies: bool = True,
        mailbox_id: str | None = None,
    ) -> uuid.UUID:
        with tenant_session(self.tenant_id) as session:
            campaign = Campaign(
                tenant_id=self.tenant_id,
                name=f"campaign-{uuid.uuid4().hex[:8]}",
                dry_run=dry_run,
                simulated_replies_enabled=simulated_replies,
                mailbox_id=mailbox_id,
            )
            session.add(campaign)
            session.flush()
            return campaign.id

    def lead(
        self,
        *,
        campaign_id: uuid.UUID | None = None,
        source_id: uuid.UUID | None = None,
        email: str | None = None,
        dry_run: bool = True,
        **overrides,
    ) -> uuid.UUID:
        campaign_id = campaign_id or self.campaign()
        source_id = source_id or self.source()
        email = email or f"lead-{uuid.uuid4().hex[:10]}@example.test"
        fields = {
            "tenant_id": self.tenant_id,
            "campaign_id": campaign_id,
            "source_id": source_id,
            "source_terms_status": "yes",
            "lawful_basis": "synthetic",
            "region_assumption": "none-synthetic",
            "email": email,
            "email_hash": hash_email(email),
            "email_domain": email_domain(email),
            "dry_run": dry_run,
        }
        fields.update(overrides)
        with tenant_session(self.tenant_id) as session:
            lead = Lead(**fields)
            session.add(lead)
            session.flush()
            return lead.id


@pytest.fixture
def factory_a(tenant_a) -> LeadFactory:
    return LeadFactory(tenant_a[0])


@pytest.fixture
def factory_b(tenant_b) -> LeadFactory:
    return LeadFactory(tenant_b[0])


# ── Journey helpers ─────────────────────────────────────────────────────────


def run_to_approval(tenant_id: uuid.UUID, lead_id: uuid.UUID) -> None:
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.stopped_on == "waiting_human", outcome


def approve_current_draft(
    tenant_id: uuid.UUID, lead_id: uuid.UUID, approver: str = "test-operator"
) -> uuid.UUID:
    with tenant_session(tenant_id) as session:
        draft = (
            session.execute(
                select(OutreachDraft)
                .where(
                    OutreachDraft.lead_id == lead_id,
                    OutreachDraft.status == "pending_approval",
                )
                .order_by(OutreachDraft.version.desc())
            )
            .scalars()
            .first()
        )
        assert draft is not None, "no pending draft to approve"
        approve_draft(session, draft=draft, approver=approver)
        return draft.id


def walk_to_sent(tenant_id: uuid.UUID, lead_id: uuid.UUID) -> None:
    """Full assisted walk: pipeline → human gate → eligibility → worker."""
    run_to_approval(tenant_id, lead_id)
    approve_current_draft(tenant_id, lead_id)
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.stopped_on == "waiting_worker", outcome
    stats = process_pending()
    assert stats.sent == 1, stats


def walk_to_closed(tenant_id: uuid.UUID, lead_id: uuid.UUID) -> None:
    walk_to_sent(tenant_id, lead_id)
    # Pin the reply intent: the hash-derived persona could just as well
    # decline or unsubscribe, and this helper promises 'closed'.
    from relay.synthetic.generator import ReplyIntent
    from relay.synthetic.seed import create_simulated_reply

    create_simulated_reply(tenant_id, lead_id, intent=ReplyIntent.INTERESTED)
    outcome = PipelineRunner(tenant_id, lead_id=lead_id).run()
    assert outcome.final_state == "closed", outcome
