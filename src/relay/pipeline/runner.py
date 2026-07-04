"""The Phase 0 pipeline runner — an empty pipeline with real bones.

Walks a lead through the full state machine with **no real work in it**:
every reasoning step is a routed stub (relay.routing.executors), every
transition goes through the state machine service, every step is counted
and billed by the guardrail harness, and each step runs in its own
transaction so a kill or crash never leaves a half-applied step.

The runner stops (returns) at the two places Phase 0 must stop:

- ``approval_pending`` — the human gate; only an explicit approval moves
  the lead onward;
- ``send_queued`` — the internal send worker owns execution.

Later phases replace step bodies (real sourcing, enrichment, scoring…)
without changing this control flow — the spine (n8n) calls the same
steps through the API.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from relay.compute.prompting import UNTRUSTED_KEY
from relay.config import get_settings
from relay.crm.sync import sync_lead
from relay.db.engine import tenant_session
from relay.db.models import (
    Campaign,
    Lead,
    OutreachDraft,
    Reply,
    SendJob,
    build_idempotency_key,
)
from relay.domain import eligibility
from relay.domain.state_machine import transition
from relay.domain.states import TERMINAL_STATES, LeadState
from relay.domain.vocab import TriageCategory
from relay.guardrails.harness import GuardrailViolation, RunHarness
from relay.logs import bind_run_context, clear_run_context, get_logger
from relay.routing.executors import execute
from relay.routing.router import TaskType


def _prospect_payload(lead: Lead) -> dict:
    """Build a task payload from a lead, §11-safe by construction.

    Short structured identity fields travel as trusted context (they are
    validated, length-bounded columns); all free prospect-authored text —
    today the bio — goes under UNTRUSTED_KEY so the prompt scaffolding
    wraps it as provenance-labeled inert data.
    """
    payload: dict = {
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "title": lead.title,
        "company": lead.company_name,
        "company_domain": lead.company_domain,
    }
    if lead.bio:
        payload[UNTRUSTED_KEY] = {"prospect_bio": lead.bio}
    return payload


log = get_logger(__name__)

ACTOR = "system:pipeline"

#: States where the runner must stop and wait for someone else.
_WAIT_STATES = frozenset(
    {
        LeadState.APPROVAL_PENDING,  # human gate
        LeadState.SEND_QUEUED,  # internal send worker
    }
)


@dataclass
class RunOutcome:
    run_id: uuid.UUID
    lead_id: uuid.UUID
    final_state: str
    steps: int
    cost_units: float
    stopped_on: str  # "waiting_human" | "waiting_worker" | "terminal" | "idle"
    visited: list[str] = field(default_factory=list)


class PipelineRunner:
    def __init__(
        self,
        tenant_id: uuid.UUID,
        *,
        lead_id: uuid.UUID,
        kind: str = "lead_journey",
        max_iterations: int | None = None,
        budget_units: float | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.lead_id = lead_id
        self.harness = RunHarness(
            tenant_id=tenant_id,
            kind=kind,
            lead_id=lead_id,
            max_iterations=max_iterations,
            budget_units=budget_units,
        )

    # ── Public API ──────────────────────────────────────────────────────────

    def run(self) -> RunOutcome:
        """Advance the lead until it waits, finishes, or a guardrail fires."""
        bind_run_context(
            run_id=self.harness.run_id,
            tenant_id=self.tenant_id,
            lead_id=self.lead_id,
        )
        visited: list[str] = []
        try:
            while True:
                state, progressed = self._advance_once()
                if progressed:
                    visited.append(state)
                    continue
                stopped_on = self._stop_reason(state)
                self.harness.complete(detail=f"stopped: {stopped_on}")
                # Mirror to the CRM after the run, outside any step
                # transaction — best-effort by contract, never a gate.
                sync_lead(self.tenant_id, self.lead_id, context="pipeline_run")
                return RunOutcome(
                    run_id=self.harness.run_id,
                    lead_id=self.lead_id,
                    final_state=state,
                    steps=self.harness.iterations,
                    cost_units=self.harness.cost_units,
                    stopped_on=stopped_on,
                    visited=visited,
                )
        except GuardrailViolation:
            # Persist the kill now — the step's session has unwound, so this
            # write does not contend with it for a pool connection.
            self.harness.finalize_kill()
            raise
        except Exception as exc:
            self.harness.fail(str(exc))
            raise
        finally:
            clear_run_context()

    # ── Step engine ─────────────────────────────────────────────────────────

    def _advance_once(self) -> tuple[str, bool]:
        """One step in its own transaction. Returns (state, progressed)."""
        with tenant_session(self.tenant_id) as session:
            lead = session.get(Lead, self.lead_id)
            if lead is None:
                raise LookupError(f"lead {self.lead_id} not found")
            state = LeadState(lead.state)
            if state in TERMINAL_STATES or state in _WAIT_STATES:
                return str(state), False
            handler = _STEP_HANDLERS.get(state)
            if handler is None:
                return str(state), False
            try:
                handler(self, session, lead)
            except _NoProgress:
                return str(state), False
            return str(lead.state), True

    @staticmethod
    def _stop_reason(state: str) -> str:
        if state == str(LeadState.APPROVAL_PENDING):
            return "waiting_human"
        if state == str(LeadState.SEND_QUEUED):
            return "waiting_worker"
        if LeadState(state) in TERMINAL_STATES:
            return "terminal"
        return "idle"

    # ── Steps (empty pipeline: routed stubs + transitions) ─────────────────

    def _step_check_source(self, session: Session, lead: Lead) -> None:
        self.harness.tick("check_source")
        execute(
            TaskType.CLASSIFICATION,
            {"what": "source_terms"},
            harness=self.harness,
        )
        transition(
            session,
            lead,
            LeadState.SOURCE_CHECKED,
            actor=ACTOR,
            run_id=self.harness.run_id,
        )

    def _step_source_decision(self, session: Session, lead: Lead) -> None:
        self.harness.tick("source_decision")
        transition(
            session,
            lead,
            LeadState.ENRICHMENT_PENDING,
            actor=ACTOR,
            reason="source register terms allow use",
            run_id=self.harness.run_id,
        )

    def _step_enrich(self, session: Session, lead: Lead) -> None:
        self.harness.tick("enrich")
        execute(TaskType.ENRICHMENT, _prospect_payload(lead), harness=self.harness)
        transition(
            session,
            lead,
            LeadState.ENRICHED,
            actor=ACTOR,
            run_id=self.harness.run_id,
        )

    def _step_request_verification(self, session: Session, lead: Lead) -> None:
        self.harness.tick("request_verification")
        transition(
            session,
            lead,
            LeadState.VERIFICATION_PENDING,
            actor=ACTOR,
            run_id=self.harness.run_id,
        )

    def _step_verify(self, session: Session, lead: Lead) -> None:
        self.harness.tick("verify_email")
        # Synthetic leads verify by construction; a real verifier tool
        # replaces this in Phase 1A/1B.
        lead.email_verified = True
        transition(
            session,
            lead,
            LeadState.VERIFIED,
            actor=ACTOR,
            reason="synthetic verification",
            run_id=self.harness.run_id,
        )

    def _step_queue_scoring(self, session: Session, lead: Lead) -> None:
        self.harness.tick("queue_scoring")
        transition(
            session,
            lead,
            LeadState.SCORING_PENDING,
            actor=ACTOR,
            run_id=self.harness.run_id,
        )

    def _step_score(self, session: Session, lead: Lead) -> None:
        self.harness.tick("score")
        result = execute(
            TaskType.FIT_SCORING, _prospect_payload(lead), harness=self.harness
        )
        score = float(result.output["fit_score"])
        # Defensive clamp: a backend cannot buy extra qualification by
        # returning 7.3 — scores live in [0, 1].
        score = min(max(score, 0.0), 1.0)
        lead.fit_score = score
        threshold = get_settings().fit_score_threshold
        if score < threshold:
            transition(
                session,
                lead,
                LeadState.SCORED_REJECTED,
                actor=ACTOR,
                reason=f"fit score {score} below threshold {threshold}",
                run_id=self.harness.run_id,
            )
            return
        transition(
            session,
            lead,
            LeadState.SCORED_QUALIFIED,
            actor=ACTOR,
            reason=f"fit score {score} >= threshold {threshold}",
            run_id=self.harness.run_id,
        )

    def _step_queue_personalization(self, session: Session, lead: Lead) -> None:
        self.harness.tick("queue_personalization")
        transition(
            session,
            lead,
            LeadState.PERSONALIZATION_PENDING,
            actor=ACTOR,
            run_id=self.harness.run_id,
        )

    def _step_personalize(self, session: Session, lead: Lead) -> None:
        self.harness.tick("personalize")
        result = execute(
            TaskType.OUTREACH_COPY, _prospect_payload(lead), harness=self.harness
        )
        next_version = (
            session.execute(
                select(func.coalesce(func.max(OutreachDraft.version), 0)).where(
                    OutreachDraft.tenant_id == lead.tenant_id,
                    OutreachDraft.lead_id == lead.id,
                )
            ).scalar_one()
            + 1
        )
        session.add(
            OutreachDraft(
                tenant_id=lead.tenant_id,
                lead_id=lead.id,
                campaign_id=lead.campaign_id,
                version=next_version,
                subject=result.output["subject"],
                body=result.output["body"],
                personalization_sources=result.output["personalization_sources"],
                status="pending_approval",
            )
        )
        transition(
            session,
            lead,
            LeadState.DRAFT_READY,
            actor=ACTOR,
            reason=f"draft v{next_version} created",
            run_id=self.harness.run_id,
        )

    def _step_submit_for_approval(self, session: Session, lead: Lead) -> None:
        self.harness.tick("submit_for_approval")
        transition(
            session,
            lead,
            LeadState.APPROVAL_PENDING,
            actor=ACTOR,
            reason="awaiting human gate",
            run_id=self.harness.run_id,
        )

    def _step_queue_eligibility(self, session: Session, lead: Lead) -> None:
        self.harness.tick("queue_eligibility")
        transition(
            session,
            lead,
            LeadState.SEND_ELIGIBILITY_PENDING,
            actor=ACTOR,
            run_id=self.harness.run_id,
        )

    def _step_eligibility_gate(self, session: Session, lead: Lead) -> None:
        """The send-eligibility gate (§10). Approval got us here; only a
        full pass on the checklist queues a send job."""
        self.harness.tick("send_eligibility_gate")
        campaign = session.get(Campaign, lead.campaign_id)
        assert campaign is not None
        draft = session.execute(
            select(OutreachDraft).where(
                OutreachDraft.tenant_id == lead.tenant_id,
                OutreachDraft.lead_id == lead.id,
                OutreachDraft.status == "approved",
                OutreachDraft.version == lead.approved_message_version,
            )
        ).scalar_one_or_none()
        if draft is None:
            transition(
                session,
                lead,
                LeadState.SEND_BLOCKED,
                actor=ACTOR,
                reason="no approved draft for current version",
                run_id=self.harness.run_id,
            )
            return

        # Mode follows *intent*, not availability. A dry-run lead/campaign
        # simulates; a real-intent one is evaluated as 'real' and therefore
        # blocked by the eligibility gate until Phase 1C infrastructure
        # exists. We must NOT silently downgrade a real-intent lead to a
        # simulated "sent" — that would record outreach that never happened
        # and burn the lead (sent is near-terminal).
        effective_dry_run = lead.dry_run or campaign.dry_run
        mode = "simulated" if effective_dry_run else "real"
        result = eligibility.evaluate(
            session, lead=lead, campaign=campaign, draft=draft, mode=mode
        )
        if not result.eligible:
            transition(
                session,
                lead,
                LeadState.SEND_BLOCKED,
                actor=ACTOR,
                reason=f"eligibility failed: {result.failure_summary()}",
                run_id=self.harness.run_id,
            )
            return

        # Transition first, then queue the job in the SAME transaction —
        # the transactional-outbox pattern. The DB trigger requires the
        # lead to be in send_queued at insert.
        transition(
            session,
            lead,
            LeadState.SEND_QUEUED,
            actor=ACTOR,
            reason=f"eligible ({mode})",
            run_id=self.harness.run_id,
        )
        sequence_step = 1
        idempotency_key = build_idempotency_key(
            lead.tenant_id,
            lead.campaign_id,
            lead.id,
            sequence_step,
            draft.version,
        )
        session.add(
            SendJob(
                tenant_id=lead.tenant_id,
                campaign_id=lead.campaign_id,
                lead_id=lead.id,
                draft_id=draft.id,
                sequence_step=sequence_step,
                message_version=draft.version,
                idempotency_key=idempotency_key,
                mode=mode,
                recipient_email_hash=lead.email_hash,
                recipient_domain=lead.email_domain,
                mailbox_id=campaign.mailbox_id,
            )
        )
        session.flush()

    def _step_after_sent(self, session: Session, lead: Lead) -> None:
        """Reply capture. Simulated mode generates one; an existing
        untriaged reply (webhook-ingested or pre-seeded) is used as-is."""
        self.harness.tick("capture_reply")
        existing = (
            session.execute(
                select(Reply).where(
                    Reply.tenant_id == lead.tenant_id,
                    Reply.lead_id == lead.id,
                    Reply.triage_category.is_(None),
                )
            )
            .scalars()
            .first()
        )
        if existing is None:
            campaign = session.get(Campaign, lead.campaign_id)
            assert campaign is not None
            if not campaign.simulated_replies_enabled:
                # Nothing to do; a real reply would arrive via webhook later.
                raise _NoProgress
            from relay.synthetic.generator import simulated_reply_text
            from relay.synthetic.seed import intent_for_lead

            job = session.execute(
                select(SendJob).where(
                    SendJob.lead_id == lead.id, SendJob.status == "sent"
                )
            ).scalar_one_or_none()
            if job is None:
                raise _NoProgress  # sent state but job not finalized yet
            intent = intent_for_lead(lead)
            session.add(
                Reply(
                    tenant_id=lead.tenant_id,
                    lead_id=lead.id,
                    campaign_id=lead.campaign_id,
                    send_job_id=job.id,
                    simulated=True,
                    subject="Re: your note",
                    body=simulated_reply_text(
                        intent, variant=int(lead.email_hash[8], 16)
                    ),
                )
            )
            session.flush()
        lead.replied_at = datetime.now(tz=UTC)
        transition(
            session,
            lead,
            LeadState.REPLY_RECEIVED,
            actor=ACTOR,
            reason="simulated reply" if existing is None else "reply on record",
            run_id=self.harness.run_id,
        )

    def _step_queue_triage(self, session: Session, lead: Lead) -> None:
        self.harness.tick("queue_triage")
        transition(
            session,
            lead,
            LeadState.TRIAGE_PENDING,
            actor=ACTOR,
            run_id=self.harness.run_id,
        )

    def _step_triage(self, session: Session, lead: Lead) -> None:
        """Triage the actual reply text — the body is prospect-authored and
        enters the prompt only under UNTRUSTED_KEY (§11)."""
        self.harness.tick("triage")
        reply = (
            session.execute(
                select(Reply)
                .where(
                    Reply.tenant_id == lead.tenant_id,
                    Reply.lead_id == lead.id,
                    Reply.triage_category.is_(None),
                )
                .order_by(Reply.received_at)
            )
            .scalars()
            .first()
        )
        if reply is None:
            raise LookupError("lead in triage_pending has no untriaged reply")

        untrusted = {"reply_body": reply.body}
        if reply.subject:
            untrusted["reply_subject"] = reply.subject
        result = execute(
            TaskType.REPLY_TRIAGE,
            {UNTRUSTED_KEY: untrusted},
            harness=self.harness,
        )
        raw_category = str(result.output["category"])
        try:
            category = TriageCategory(raw_category)
        except ValueError:
            # A backend inventing categories does not get to steer the
            # state machine. Opt-out is the safe direction (§10).
            log.warning("triage returned unknown category", category=raw_category)
            category = TriageCategory.UNSUBSCRIBED
        confidence = result.output.get("confidence")
        reply.triage_category = str(category)
        if isinstance(confidence, int | float):
            reply.triage_confidence = min(max(float(confidence), 0.0), 1.0)

        target = {
            TriageCategory.INTERESTED: LeadState.INTERESTED,
            TriageCategory.NOT_INTERESTED: LeadState.NOT_INTERESTED,
            TriageCategory.UNSUBSCRIBED: LeadState.UNSUBSCRIBED,
        }[category]
        transition(
            session,
            lead,
            target,
            actor=ACTOR,
            reason=f"reply triaged: {category}",
            run_id=self.harness.run_id,
        )

    def _step_queue_booking(self, session: Session, lead: Lead) -> None:
        self.harness.tick("queue_booking")
        transition(
            session,
            lead,
            LeadState.BOOKING_PENDING,
            actor=ACTOR,
            run_id=self.harness.run_id,
        )

    def _step_book(self, session: Session, lead: Lead) -> None:
        self.harness.tick("book")
        lead.booking_ref = f"sim-cal-{uuid.uuid4().hex[:12]}"
        transition(
            session,
            lead,
            LeadState.BOOKED,
            actor=ACTOR,
            reason="simulated calendar booking",
            run_id=self.harness.run_id,
        )

    def _step_close(self, session: Session, lead: Lead) -> None:
        self.harness.tick("close")
        transition(
            session,
            lead,
            LeadState.CLOSED,
            actor=ACTOR,
            run_id=self.harness.run_id,
        )


class _NoProgress(Exception):
    """Internal: a handler decided there is nothing to do."""


_STEP_HANDLERS = {
    LeadState.CREATED: PipelineRunner._step_check_source,
    LeadState.SOURCE_CHECKED: PipelineRunner._step_source_decision,
    LeadState.ENRICHMENT_PENDING: PipelineRunner._step_enrich,
    LeadState.ENRICHED: PipelineRunner._step_request_verification,
    LeadState.VERIFICATION_PENDING: PipelineRunner._step_verify,
    LeadState.VERIFIED: PipelineRunner._step_queue_scoring,
    LeadState.SCORING_PENDING: PipelineRunner._step_score,
    LeadState.SCORED_QUALIFIED: PipelineRunner._step_queue_personalization,
    LeadState.PERSONALIZATION_PENDING: PipelineRunner._step_personalize,
    LeadState.DRAFT_READY: PipelineRunner._step_submit_for_approval,
    LeadState.APPROVED: PipelineRunner._step_queue_eligibility,
    LeadState.SEND_ELIGIBILITY_PENDING: PipelineRunner._step_eligibility_gate,
    LeadState.SENT: PipelineRunner._step_after_sent,
    LeadState.REPLY_RECEIVED: PipelineRunner._step_queue_triage,
    LeadState.TRIAGE_PENDING: PipelineRunner._step_triage,
    LeadState.INTERESTED: PipelineRunner._step_queue_booking,
    LeadState.BOOKING_PENDING: PipelineRunner._step_book,
    LeadState.BOOKED: PipelineRunner._step_close,
}
