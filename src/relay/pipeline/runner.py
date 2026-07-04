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

from relay.config import get_settings
from relay.db.engine import tenant_session
from relay.db.models import Campaign, Lead, OutreachDraft, SendJob
from relay.domain import eligibility
from relay.domain.state_machine import transition
from relay.domain.states import TERMINAL_STATES, LeadState
from relay.guardrails.harness import GuardrailViolation, RunHarness
from relay.logs import bind_run_context, clear_run_context, get_logger
from relay.routing.executors import execute
from relay.routing.router import TaskType

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
            # The harness already persisted the kill status.
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
        execute(TaskType.ENRICHMENT, harness=self.harness)
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
        result = execute(TaskType.FIT_SCORING, harness=self.harness)
        lead.fit_score = result.output["fit_score"]
        transition(
            session,
            lead,
            LeadState.SCORED_QUALIFIED,
            actor=ACTOR,
            reason=f"stub fit score {result.output['fit_score']}",
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
        result = execute(TaskType.OUTREACH_COPY, harness=self.harness)
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

        settings = get_settings()
        effective_dry_run = lead.dry_run or campaign.dry_run
        mode = (
            "real"
            if (not effective_dry_run and settings.real_send_enabled)
            else "simulated"
        )
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
        idempotency_key = (
            f"{lead.tenant_id}:{lead.campaign_id}:{lead.id}:1:{draft.version}"
        )
        session.add(
            SendJob(
                tenant_id=lead.tenant_id,
                campaign_id=lead.campaign_id,
                lead_id=lead.id,
                draft_id=draft.id,
                sequence_step=1,
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
        """Simulated reply capture — only in explicit seed/test mode."""
        self.harness.tick("simulate_reply")
        campaign = session.get(Campaign, lead.campaign_id)
        assert campaign is not None
        if not campaign.simulated_replies_enabled:
            # Nothing to do; a real reply would arrive via webhook later.
            raise _NoProgress
        lead.replied_at = datetime.now(tz=UTC)
        transition(
            session,
            lead,
            LeadState.REPLY_RECEIVED,
            actor=ACTOR,
            reason="simulated reply (seed/test mode)",
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
        self.harness.tick("triage")
        result = execute(TaskType.REPLY_TRIAGE, harness=self.harness)
        category = result.output["category"]
        target = {
            "interested": LeadState.INTERESTED,
            "not_interested": LeadState.NOT_INTERESTED,
            "unsubscribed": LeadState.UNSUBSCRIBED,
        }[category]
        transition(
            session,
            lead,
            target,
            actor=ACTOR,
            reason=f"stub triage: {category}",
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
