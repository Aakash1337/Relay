# RELAY — Project Documentation

**RELAY is an autonomous B2B sales prospecting and outreach system.**
Tagline: *Outbound that compounds.*

This document explains what RELAY is meant to do, how it is designed to do it, and the reasoning behind the major architectural decisions. It is written to be self-contained: a reader with no prior context should be able to understand the project from this document alone.

**Related documents:** the companion *Development Roadmap* sequences this scope into phases with testable exit criteria and marks what belongs in the MVP versus later. This document is the stable architecture reference; the roadmap is the build plan.

> **Status note.** This spec has been through a pre-build hardening pass. Several items below (data sourcing, lawful basis, suppression, send eligibility, tenant isolation, injection controls) exist because operating this system on *real prospects* is a legal and reputational undertaking, not just an engineering one. Anything describing external provider terms, sender requirements, or licensing must be **re-verified against current sources at build time** — those rules change, and this document is a checklist of things to confirm, not a source of settled truth about them.

---

## 1. Overview

Outbound sales prospecting is repetitive, high-volume, and leaky. A team spends most of its time on mechanical work — finding companies that fit, locating and verifying contacts, researching each prospect, scoring fit, drafting personalized messages, triaging replies, and booking calls — and only a small fraction of that effort reaches a qualified conversation. The manual funnel loses value at every stage.

RELAY automates that funnel end to end as an orchestrated pipeline. It sources prospects, enriches and qualifies them, drafts personalized outreach, routes replies, and books meetings, running largely unattended with a human checkpoint at the moment that matters most (sending). The design goal is not just automation but *compounding*: every run produces data (what got a reply, what converted, which segments respond) that feeds back into scoring and personalization, so the system gets better the longer it runs. That self-improving loop is the intended differentiator — but see §12 on why it is a hypothesis to be proven, not an automatic moat.

RELAY is built as a product for a business context (a client-facing sales-automation deliverable), so reliability, cost predictability, legal defensibility, and safety around live outbound are first-class concerns, not afterthoughts.

---

## 2. Goals and Non-Goals

**Goals**

- Automate the full prospecting-to-booking funnel as a repeatable, observable pipeline.
- Keep a human approval gate on anything that sends outbound, for both safety and quality.
- Make unlawful, suppressed, or duplicate sends *structurally impossible*, not merely unlikely.
- Run at low and predictable cost, with the high-volume work handled cheaply.
- Improve over time by feeding outcome data back into scoring and personalization.
- Be resilient to failure: no single fault should cause a runaway, a silent bad send, or a cross-tenant leak.

**Non-Goals**

- RELAY is not a CRM, not a frontend dashboard, and not a data provider. It is the automation and decision layer between those systems.
- It does not bypass platform terms of service to obtain data (no scraping of prohibited sources).
- It is not intended to send fully unattended at launch; the human gate is deliberate.

---

## 3. System Architecture

RELAY separates three distinct concerns. Keeping them separate is the central design decision, because each has different requirements for reliability, speed, and correctness.

**1. The deterministic spine (workflow orchestration).**
A workflow engine (n8n) is the backbone. It defines the pipeline as explicit, ordered steps, handles scheduling and workflow-level retries, and provides a reliable, inspectable control flow. The spine is dumb on purpose: it does no reasoning, it just moves work through the pipeline predictably. *(Commercial licensing for client-facing use must be confirmed — see §15.)*

**2. The reasoning layer (the planner).**
At specific decision points, the spine calls a reasoning component that does the actual judgment: researching a prospect, scoring fit, drafting a message, handling an ambiguous reply. It plans and sequences the harder sub-tasks and calls tools as needed. It is where the "thinking" happens and the part most sensitive to correctness. Critically, the planner **advises**; it never directly performs irreversible actions (see §10).

**3. The canonical datastore (state).**
A single source of truth (a relational database) holds the authoritative state of every lead. Every component reads and writes lead state here rather than passing state informally. A queue and a vector store support it but do not replace it.

**Tenant isolation is foundational, not a later feature.** Every table carries a `tenant_id` from the first schema. Suppression, idempotency keys, logging, encryption keys, vector-store namespaces, and CRM/sending credentials are all tenant-scoped from day one. Full self-serve multi-tenancy is a later phase, but the *isolation primitives* are built in Phase 0 — retrofitting them later is a rewrite, and a cross-tenant leak in a system that contacts real people is a serious incident.

**Supporting layers.**

- **Tool-integration layer.** Tool servers expose external capabilities to the planner through a uniform interface: lead search, enrichment, verification, email delivery, calendar booking, and CRM sync. New capabilities are new tool servers; pipeline logic does not change.
- **Backend API.** A service layer (FastAPI) exposes RELAY's actions as validated HTTP endpoints.

**High-level flow**

```
        Trigger / schedule
               │
               ▼
     Workflow spine (n8n)  ──────────────┐
               │                         │
        at decision points               │  read/write lead state
               ▼                         ▼
       Reasoning layer  ───────►  Canonical datastore (DB)
               │                         ▲
        calls tools (scoped)             │
               ▼                         │
     Tool-integration layer  ────────────┘
      (search, enrich, verify,
       send-job, book, CRM)
               │
               ▼
     External services / mailboxes / calendar
```

**Control pattern.** The spine controls *what happens when*; the planner is invoked as a structured service at defined decision points and returns a result the spine acts on. A separate, read-mostly supervisory loop observes the pipeline and surfaces insights without silently mutating live state.

**The human gate.** Anything that sends outbound passes a human approval checkpoint. This is both a safety control and a product feature — but approval alone does not send (see §10).

---

## 4. The Lead State Machine

The pipeline is a state machine over the canonical datastore. It is specified here rather than left implicit, because the safety of the whole system depends on which transitions are *possible*. States and transitions are enforced in code **and** database constraints, not by the planner.

**States (minimum set):**

```
created → source_checked → {source_rejected | enrichment_pending}
enrichment_pending → enriched → verification_pending
verification_pending → {verification_failed | verified}
verified → scoring_pending → {scored_rejected | scored_qualified}
scored_qualified → personalization_pending → draft_ready → approval_pending
approval_pending → {rejected_by_human | approved}
approved → send_eligibility_pending → {send_blocked | send_queued}
send_queued → sent
sent → {bounce_received | reply_received}
reply_received → triage_pending → {unsubscribed | not_interested | interested}
interested → booking_pending → booked → closed
(any) → {error_retryable | error_terminal}
```

**Critical invariants (enforced structurally):**

- `sent` requires `approved` **and** `send_eligible` (see §10).
- `send_eligible` requires: not suppressed, verified email, current message-version approval, tenant/mailbox match.
- `reply_received` cannot occur for dry-run leads except in explicit seed/test mode.
- `booked` requires a linked reply / person / calendar event.
- `unsubscribed` is terminal for all future marketing and sales sends.
- A retryable error cannot retry past its cap.
- A lead cannot be in two active campaign send states simultaneously.
- **No cross-tenant transition is ever possible.**

A `dry_run` / `test_mode` flag is a first-class field on every lead and campaign, and the send path is physically incapable of executing a real send when it is set.

---

## 5. The Pipeline

The funnel, in order: **sourcing → verification → scoring/qualification → personalization → human approval gate → send-eligibility gate → send → reply triage → booking.** Outcome data from every stage flows back into scoring and personalization (subject to the learning discipline in §12).

Note that two gates sit before any message leaves: the **human gate** (a person approves the content) and the **send-eligibility gate** (code and DB verify the send is lawful, suppression-clear, authenticated, and non-duplicate). Both must pass.

---

## 6. CRM and System-of-Record Integration

A CRM (HubSpot, Salesforce, or an open-source equivalent) holds the human-facing view of contacts, companies, activity, and deals. The integration hinges on one decision: **is the CRM RELAY's system of record, or a destination RELAY writes to?**

RELAY keeps its own canonical datastore authoritative and treats the CRM as a synced peer. A CRM's data model is built for human workflows, not for an autonomous pipeline's fine-grained internal state (attempt counters, intermediate state, dedup keys, guardrail counters). So:

- RELAY's datastore owns *"where is this lead in RELAY's process"* — the state machine.
- The CRM owns *"the human-facing sales record"* — contact, company, activity, deal stage, notes.

RELAY writes meaningful events to the CRM (contacted, replied, booked, qualified) and optionally reads from it as an enrichment/sourcing input.

**The CRM is not the sending provider.** This separation is important and easy to conflate. A CRM sync target and an outbound *sending provider* are different decisions with different terms of service. Some CRM marketing tools and some mailbox providers explicitly prohibit cold or purchased-list outreach, or require verifiable opt-in, regardless of what your pipeline does. Assuming "the CRM can also send the cold outreach" is a compliance and account-suspension risk.

**Sending Provider Decision Record** (must be answered before any `send-job` executor exists — verify current terms):

- Is the provider for one-to-one sales email, bulk marketing, or transactional mail?
- Does it permit cold commercial outreach? Does it require opt-in?
- Does it support unsubscribe headers / one-click unsubscribe?
- Does it expose bounce, complaint, block, and delivery events?
- Can suppression be enforced before every send?
- Can per-tenant domains/mailboxes be isolated?
- What are its suspension triggers?

**CRM sync contract.** Field ownership (who wins when RELAY and a human both edit), deterministic record matching/merge (or you get duplicates and double-outreach), rate-limit-respecting backpressure, and treating CRM free-text as untrusted input (§11). Built CRM-first behind a swappable adapter, since the client's existing CRM usually dictates the choice.

---

## 7. Data Sourcing, Lawful Basis, and Compliance

**Compliance starts at sourcing, not sending.** This is the single most important correction in this spec. The moment RELAY sources, enriches, stores, or CRM-syncs a *real, named* person, it is processing personal data — even if no message is ever sent. Treating "dry-run = no compliance needed" is wrong. Only **synthetic data, your own seed contacts, or contacts with explicit test consent** are compliance-free for early testing.

**Controller vs. processor — an open decision that must be resolved before real-prospect ingestion.** For each client deployment, determine whether RELAY (you/Cybic) is the *controller* of prospect data or the *processor* acting on the client's behalf. This determines who carries the heavy compliance obligations and shapes contracts, notices, and liability. It is deliberately left open here because it is a per-engagement decision, not a default.

**Lead Source Register.** "No prohibited scraping" is a policy statement, not a control. Every data source gets a register entry:

| Field | Answer |
| --- | --- |
| Source name / type | Apollo, HubSpot CRM, client CSV, public registry, website, … / API, uploaded list, licensed provider, CRM import |
| Terms allow this use? | yes / no / legal review needed |
| Personal data collected | name, work email, title, company, … |
| Region restrictions | US-only, no EU, no Canada, … |
| Proof of lawful use | contract, consent, legitimate-interest assessment, client warranty |
| Deletion mechanism | can records be removed from source, RELAY, CRM, vector store? |
| Confidence score | data reliability |
| Suppression checked before import? | yes / no |

**Hard rule:** no prospect enters the canonical datastore without `source_id`, `source_terms_status`, `lawful_basis`, and `region_assumption`.

**Jurisdiction matrix.** Maintain a per-region position (US / Canada / UK / EU / other in scope) covering lawful basis, disclosure/notice, unsubscribe, and retention. Requirements differ materially by region (e.g. US CAN-SPAM, Canada CASL, UK/EU GDPR); confirm current rules at build time and encode them, don't assume them.

**Retention and data-subject requests.** Define retention windows and a deletion / right-to-be-forgotten workflow that removes records from the datastore, CRM, *and* vector store — while retaining a hashed suppression entry so a deleted contact is never re-contacted.

---

## 8. Compute and Task-Routing Strategy

RELAY does work ranging from trivial to genuinely hard, and does the trivial work far more often. The strategy is a **two-tier split**.

**Local compute tier** (self-hosted model, local hardware) handles high-volume, bounded sub-tasks: enrichment, classification, extraction, summarization, tagging. Cheap (electricity only) and where most volume lives.

**Hosted compute tier** (provider over an API) handles work where being subtly wrong is expensive: orchestration/planning decisions that cascade, anything security-sensitive, and customer-facing output like outreach copy.

**Why the split matters.** RELAY runs unattended, so a mistake a human would catch instantly can quietly compound across a run. Put the most reliable compute where errors cascade; put the cheapest adequate compute where work is bounded and high-volume.

**Routing guide** (task → tier → extended reasoning):

| Task | Tier | Extended reasoning |
| --- | --- | --- |
| Enrichment, field extraction | Local | Off |
| Classification, tagging | Local | Off |
| Page/prospect summarization | Local | Off |
| Fit scoring (ambiguous) | Hosted / local + review | On for hard cases |
| Reply triage | Local (simple) / Hosted (ambiguous) | Situational |
| Outreach copy (customer-facing) | Hosted, or local + human review | On |
| Orchestration / planning | Hosted | On |
| Anything touching credentials or untrusted input | Hosted | On |

**Extended-reasoning mode** is toggled per task, hard-coded per type (enrichment off, planning on), with the planner overriding only at the margins — for cost as well as speed.

**Open validation item.** The local tier is trusted for text-in/text-out today. Before it is trusted to *call tools* autonomously, its tool-calling reliability must be validated separately; until then, tool-calling steps route to the hosted tier.

---

## 9. Guardrails and Reliability

The failure mode to defend against is not a crash — it is **confident, expensive persistence**: a loop that keeps going while stuck.

- **Smart fallbacks — inside the planner.** Handle *expected* failures gracefully ("this source failed, try another, then give up cleanly").
- **Dumb limits — outside the planner, in the harness.** Hard mechanical stops that hold even if the planner is the thing malfunctioning: **max-iteration counter** (the single most important guardrail), **retry caps with backoff**, **per-run budget ceiling** (compute/time), **deduplication/idempotency**, and a **provider-side spend cap** as the final backstop.

Mental model: the planner does *smart* recovery; the harness enforces *dumb* safety. The dumb limits are what actually save money, because they work when the intelligent component is broken.

---

## 10. Suppression, Send Eligibility, and the Send Path

This is the most dangerous part of the system and is engineered so that a bad send is *structurally impossible*, not merely discouraged.

**The send path is split, and approval does not send.**

- `POST /outreach-drafts/{id}/approve` — moves a draft to `approved`. It does **not** send.
- An **internal-only send worker** picks up approved drafts, re-checks every invariant at execution time, and only then sends.

**Send Eligibility Gate** — a message can send only if *all* are true, checked in code and DB immediately before execution:

- contact not suppressed; contact has a lawful send basis for its region;
- sender identity approved; sending domain has SPF/DKIM/DMARC configured;
- mailbox active and below volume cap; campaign below complaint/bounce threshold;
- message includes required sender identity and unsubscribe mechanism;
- human approval exists for *that exact message version*;
- idempotency key unused; tenant and mailbox match; provider terms allow the send type.

**Idempotency key:** `tenant_id + campaign_id + lead_id + sequence_step + approved_message_version`, backed by a **database unique constraint** so duplicate sends cannot occur even under replayed webhooks, workflow bugs, or races.

**Suppression Contract.** Suppression is explicit about scope and precedence:

```
suppression_id, tenant_id,
scope: tenant | global | domain | mailbox | campaign,
email_hash, raw_email_encrypted?, domain?,
reason: unsubscribe | complaint | hard_bounce | manual | legal_delete | do_not_contact,
source: reply | link | CRM | manual | provider_webhook | import,
created_at, created_by, expires_at?,
applies_to_marketing: bool, applies_to_sales: bool
```

Open scope decisions to settle: is suppression per-tenant or global? If someone unsubscribes from Client A, can Client B contact them? Bounces and complaints auto-suppress. Deletion retains a hashed suppression entry.

**Hard invariant:** *a suppressed recipient can never enter `send_eligible`, regardless of planner output, CRM state, campaign state, or human approval.*

---

## 11. Security Considerations

RELAY touches credentials, untrusted external content, and live outbound.

**Prompt injection — engineered, not just "sanitized."** The planner reads websites, CRM notes, emails, and replies, any of which can carry adversarial instructions. Sanitization alone is insufficient; the system separates **data from instructions**:

- every external text chunk carries a provenance label (website, CRM note, email reply, enrichment provider, human operator, system config);
- untrusted text is wrapped/quoted as *data*, never merged into instructions;
- tools are scoped per task; the planner never holds broad credentials;
- sensitive actions require deterministic policy checks *outside* the model;
- model output must conform to typed schemas, and tool calls are validated by code, not by the model;
- any instruction embedded in a prospect page, CRM note, or inbound reply that tries to change system behavior is ignored;
- personalization records the source facts it used, for reviewer audit.

**Secrets and credentials.** Beyond "not in code or logs": envelope encryption for stored OAuth tokens, KMS-managed keys, token-scope minimization, refresh-token rotation, a rotation runbook, break-glass access, **per-tenant credential isolation**, an audit log of credential access, no secrets in workflow-engine execution logs, and redaction tests.

**Mailbox authorization** uses host-minted tokens with least privilege. **Outbound protection** uses rate limiting and backpressure to protect deliverability and avoid account suspension.

Security review of guardrails, credential flows, injection controls, and the send path is a mandatory human step before anything runs unattended.

---

## 12. The Feedback Flywheel — Hypothesis, Not Guarantee

The intended moat is accumulated outcome data improving scoring and personalization. This is plausible but not automatic: early outbound data is sparse, noisy, and confounded (a low reply rate could be ICP, offer, timing, deliverability, subject line, personalization, data source, sender identity, or just small sample). Without experiment design, the loop can learn the wrong lesson.

Before the learning system is built, define: the outcome taxonomy (delivered, bounced, replied, positive reply, meeting booked, qualified, opportunity, closed-won); the attribution window; control/holdout groups; minimum sample thresholds before any automatic change; segment-level confidence; and a human-edit taxonomy (factual, tone, ICP mismatch, weak proof, compliance). **Rule:** no automatic scoring/personalization change from outcomes until the minimum data threshold is met. Also decide whether feedback data may be shared across clients or only within one tenant.

---

## 13. Production Readiness Considerations

The prototype is the seed of a system a real company relies on. The gaps that sink real deployments are mostly operational, legal, and infrastructural, not the reasoning. Each is tagged for when it must land; the sequenced plan lives in the roadmap.

**Critical:**

- **Email deliverability** *(MVP if sending real mail)* — dedicated authenticated domains (SPF/DKIM/DMARC), warmup, pacing, bounce/complaint handling, reputation monitoring. A deep specialty; the send path is not built as a real-send path until these are baked into the state machine.
- **Legal/compliance** *(gates real-prospect ingestion, per §7)* — lawful basis, authoritative suppression, unsubscribe, retention, DSR, regional rules, controller/processor role.
- **Observability** *(foundation in MVP)* — structured logging, per-lead tracing, metrics, dashboards, alerting on failures and spend.
- **Testing and evaluation** *(foundation in MVP)* — software tests plus reasoning-quality evals that catch regressions when a prompt or model changes.
- **Human-in-the-loop at scale** *(gate in MVP, scale later)* — batching, confidence-based routing, and human edits fed back as signal via a defined **reviewer rubric** (approve / approve-with-edit / reject + reasons: wrong person, bad fit, bad personalization, compliance risk, tone, factually wrong, duplicate, should-not-contact). A rubric-less UI poisons the feedback loop with inconsistent labels.

**Design around now, build later:** multi-tenancy productization (primitives in Phase 0, §3); self-serve onboarding; cost attribution and per-tenant quotas; disaster recovery (backups + tested restore + clean mid-run resume).

**Also:** secrets rotation; audit trail of approvals and sends; data-ownership/offboarding; and — for client production — an SLA, uptime ownership, and incident-response process.

---

## 14. Interface — Example Endpoints

- `POST /search-leads` — source prospects for a target profile.
- `POST /score-lead` — score/qualify a prospect.
- `POST /outreach-drafts/{id}/approve` — approve a draft (does **not** send).
- *(internal)* send worker — executes approved, eligible sends after re-checking all invariants; not a public action endpoint.
- `GET /campaigns/{id}/status` — campaign state.
- `GET /health` — health check.

---

## 15. Deployment and Infrastructure

RELAY is a multi-container stack (workflow engine, tool servers, database, queue, vector store) coordinated via Docker.

- **Prototype (self-hosted).** Runs on a local server (Ryzen 9 9900X, TrueNAS/HexOS + Docker); a local 16GB GPU hosts the local compute tier; inbound webhooks via a secure tunnel (Cloudflare Zero Trust); admin over WireGuard. Marginal cost near zero.
- **Client production is different, and self-hosting is prototype-only unless a client knowingly accepts it.** Storing a *client's* prospect PII and OAuth tokens on home infrastructure is a materially higher risk than running your own experiments. Client production should use a hardened VPS or managed environment with tested offsite backups, monitoring, disk and log encryption, a patching cadence, defined uptime ownership/SLA, and no single-point-of-failure tunnel.
- **Cost note.** Infrastructure is cheap; metered hosted-tier compute is the real cost center, which is why volume routes local.
- **Concurrency note.** The local tier is a single GPU, so local inference serializes under heavy parallel load — fine for moderate/batch volume; scale moves the hot path to the hosted tier or more hardware.

**Decision records to complete before client-facing use** (verify current terms): the **Sending Provider Decision Record** (§6) and a **Workflow Engine Decision Record** for n8n — are clients exposed to n8n, are client credentials stored in your instance, are workflows client-specific, are you selling access to n8n-powered automation, and does that require Enterprise or Embed licensing? Confirm against n8n's current license terms; do not assume.

---

## 16. Build Approach

- **Spec first.** Work from concrete specs (task types, tier routing, guardrails, tool integrations, the four artifacts in the roadmap), not "build the whole thing."
- **Generated scaffolding is fine; review is mandatory.** Take boilerplate at face value; review the guardrails, the send path, suppression/eligibility, credential flows, injection controls, and tenant isolation line by line — that is where quiet, costly bugs hide.
- **Build the routing seam and tenant primitives from day one.** Both are painful to retrofit.
- **Foundations before features.** Follow the phased roadmap; each phase has a testable exit gate and assumes earlier foundations are proven.

---

## 17. Open Questions / To Validate

- **Controller vs. processor** for the first real deployment (who owns prospect-data liability).
- **Regions in scope** for the first real pilot.
- **Will the first pilot use real prospect data? Will it send real mail?**
- **Allowed lead sources** (the Lead Source Register, populated).
- **Sending provider** that permits the intended use case, and who owns domain/mailbox reputation risk.
- **Suppression scope** — per-tenant or global. *(Decided 2026-07-05: per-tenant default; global scope stays honored across every tenant — over-suppression is the safe direction — but creating a global entry is admin-only; RLS rejects it from the application role.)*
- **Cross-client feedback data** — shareable or tenant-only.
- **n8n licensing** fit for the client-hosted / client-credential model.
- **Local tool-calling reliability** — before trusting the local tier with tools unattended.
- **Single-GPU concurrency ceiling** and when the hot path must move.
- **Target cost per qualified meeting**, and the metric that kills the project.
- **Multi-step sequences** — implemented (2026-07-05, same-day un-deferral by operator decision for the prototype). Design: step N+1 re-enters the existing pipeline loop (`sent → personalization_pending`) after the campaign's `sequence_delay_hours` with no reply; every step drafts its own version and needs its own human approval (§10); cancellation is structural — a reply, bounce, or unsubscribe moves the lead out of `sent`, and suppression blocks the next step at eligibility. The idempotency check is generalized to the step being queued; the DB uniqueness constraint already covered it.

---

## 18. Glossary

- **Spine / workflow engine** — deterministic backbone sequencing the pipeline; no reasoning.
- **Planner / reasoning layer** — makes judgments at decision points; advises, never directly performs irreversible actions.
- **Worker (local tier)** — cheap, high-volume compute for bounded sub-tasks.
- **Hosted tier** — metered, high-reliability compute for cascading/sensitive/customer-facing work.
- **Canonical datastore** — the single authoritative record of lead state.
- **System of record** — the authoritative source for a piece of data; RELAY's datastore for pipeline state, the CRM for human-facing records.
- **State machine** — the enforced set of lead states and legal transitions.
- **Send-eligibility gate** — the code/DB check that must pass immediately before any send.
- **Suppression list / contract** — the authoritative do-not-contact set and the rules governing its scope and precedence.
- **Idempotency** — repeating an action has no additional effect; here, a DB constraint makes duplicate sends impossible.
- **Controller / processor** — the party legally responsible for personal data vs. the party processing it on the controller's behalf.
- **Lawful basis** — the legal justification for processing a person's data.
- **Lead Source Register** — the per-source record of terms, provenance, and lawful use.
- **Provenance label** — the origin tag on external text used to separate data from instructions.
- **Tenant / multi-tenancy** — isolated per-client data and sending; primitives from Phase 0, full product later.
- **Harness** — the scaffolding enforcing mechanical limits around the planner.
- **Deliverability** — getting mail to inboxes: reputation, authentication (SPF/DKIM/DMARC), warmup, pacing.
- **Eval harness** — tests measuring reasoning quality and catching regressions.
- **Human gate** — the content-approval checkpoint before send.
- **Flywheel** — the self-improving loop; a hypothesis to be validated, not an automatic moat.
