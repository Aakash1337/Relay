# RELAY — Development Roadmap

This is the build plan for RELAY. It sequences the scope in the *Project Documentation* into phases you can build and verify one at a time.

**How to use this roadmap.** Build phases in order. Each phase has an **exit gate** — a concrete, testable checklist. Don't start a phase until the previous gate passes, because every phase assumes the earlier foundations are proven. Within a phase, reorder freely; between phases, don't skip.

**Guiding principle.** Foundations before features. The most expensive mistakes come from building sophisticated behavior on an unverified base — a scaling feature on a pipeline that silently mis-scores, an autonomous loop with no working kill switch, a real send with no working suppression check. Each gate catches that before it compounds.

> **On the hardening pass.** This roadmap was revised after a pre-build audit. The key change: **legal and commercial de-risking gates *real data and real sending*, but does not block the synthetic Phase 0.** You can build the entire synthetic foundation while the legal/commercial questions are still open. What you cannot do is ingest real prospects or send real mail until the corresponding gates pass. Anything referencing provider terms, sender requirements, or licensing must be re-verified against current sources at build time.

---

## The four pre-build artifacts

Create these alongside the early phases. Only the **legal** one hard-blocks real-prospect work; the others inform decisions but do not block synthetic Phase 0.

1. **Legal / Data Preflight** — jurisdiction matrix, lawful-basis/consent model per region, controller-vs-processor role, data-source provenance rules, privacy notice, retention policy, DSR/deletion workflow, allowed-source list. *(Blocks real-prospect ingestion.)*
2. **State Machine + Send-Eligibility Spec** — the states, transitions, invariants, and the eligibility checklist from Project Documentation §4 and §10, made concrete.
3. **Lead Source + Sending Provider Policy** — the populated Lead Source Register and the Sending Provider Decision Record. *(Blocks real sending.)*
4. **Commercial Thesis + Unit Economics** — target customer/user, use case, who owns compliance/domains/mailboxes, willingness to pay, and a rough **cost-per-qualified-meeting** model with a written kill criterion. *(Lighter here than for an independent SaaS, since this is a client deliverable and the client partly fixes the ICP and often owns compliance — but the unit-economics math is still worth doing before much code.)*

---

## The MVP fork, restated correctly

"Dry-run" does **not** mean "no compliance." Sourcing, enriching, storing, or CRM-syncing a real named person is already processing personal data. So:

- **Compliance-free testing** uses synthetic data, your own seed contacts, or contacts with explicit test consent.
- **Real-prospect ingestion** (even with no sending) is gated behind the Legal/Data Preflight.
- **Real sending** additionally requires deliverability, suppression, send-provider approval, and audit trail.

The recommended order: prove the pipeline on synthetic/seed data first, then introduce real data behind the legal gate, then enable real sending behind the deliverability/compliance gate.

---

## Phase 0 — Foundations & Scaffolding

*Goal: a skeleton that runs end to end with no real work in it, so every later phase has solid ground. Not blocked by the legal/commercial artifacts — this is entirely synthetic.*

**Build**

- Repo, environments, config, secret storage (no secrets in code).
- **Tenant-aware schema from day one:** `tenant_id` on every table; tenant-scoped idempotency, suppression, logging, keys, and vector-store namespaces.
- **The formal state machine** (Project Documentation §4) with transitions enforced in code and DB constraints.
- Core tables: leads, **suppression**, **outbox / send-job**, **audit log**, **lead source register**; plus data-retention, region, and lawful-basis fields on leads.
- Backend API skeleton + health check; workflow engine reading/writing the datastore.
- The **task-routing seam**, stubbed (local vs hosted per task).
- The **guardrail harness**: max-iteration counter, per-run budget ceiling, dedup — with idempotency DB constraints.
- **Structured logging from line one** (observability foundation); PII redaction rules in logs.
- A hard **`dry_run` / `test_mode` flag that cannot accidentally send.**

**Exit gate**

- An empty pipeline moves a fake lead through every state; the log traces its full journey.
- A forced infinite loop is stopped by the iteration cap; an over-budget run by the budget ceiling. (Kill switches proven before anything autonomous runs.)
- Reprocessing the same lead is a no-op; the idempotency DB constraint rejects a duplicate.
- No code path can send while `dry_run` is set.
- A cross-tenant read/transition is rejected.

---

## Phase 1 — MVP Pipeline (split by data/sending risk)

Phase 1 is split into three modes so "dry-run" is never mistaken for "legally irrelevant." Each is its own gate.

### Phase 1A — Synthetic / Seed Dry-Run
*Prove pipeline mechanics with zero real PII.*

- Synthetic prospects (Faker + local model) or owned seed inboxes; simulated replies.
- Full funnel: source → verify → score → personalize → human gate → dry-run send → simulated reply capture.
- Two-tier routing made real; extended-reasoning defaults per task type.
- One CRM integration as a sync target, using **test CRM records** (self-hosted open-source CRM is ideal here).
- Minimal human approval UI with the **reviewer rubric** (approve / approve-with-edit / reject + structured reasons); every edit/rejection captured as structured data.

*Exit gate:* the synthetic funnel runs end to end; nothing reaches "send" without the human gate; no send-capable path executes without passing suppression + eligibility checks; human edits are captured as structured data; a projected **cost-per-qualified-meeting** is computed from the run and is below your predefined threshold under conservative assumptions.

### Phase 1B — Real-Data, No-Send Pilot
*Test sourcing/enrichment/scoring/draft quality on real prospects — still no sending.*

- **Requires the Legal/Data Preflight artifact to pass first.**
- Real prospects may be ingested only from registered, lawful sources; every record carries `source_id`, `source_terms_status`, `lawful_basis`, `region_assumption`.
- No sending. Draft quality evaluated against the rubric.

*Exit gate:* real prospects flow through source→score→draft with provenance on every record; no send path is reachable; deletion/DSR removes a record from datastore, CRM, and vector store while leaving a hashed suppression entry.

### Phase 1C — Tiny Real-Send Pilot
*The first real emails — volume-capped, every send manually approved.*

- **Requires deliverability basics, suppression, send-provider approval, and audit trail.**
- Every send passes the full **send-eligibility gate**; every send is human-approved; volume is capped.

*Exit gate:* a handful of real, eligible, approved, non-duplicate sends go out through an approved provider; suppression and unsubscribe verified to work end to end; every send audited.

---

## Phase 2 — Reliability, Observability & Evaluation

*Goal: make the proven pipeline trustworthy unattended, and measurable enough to change safely.*

**Build**

- Harden and **test** the full guardrail suite.
- **Observability proper:** dashboards, metrics (throughput, error/reply rates, cost per run), per-lead tracing, alerting on failures and spend spikes.
- **Evaluation harness:** software tests plus reasoning-quality evals that flag regressions on prompt/model change.
- **Resumability:** clean recovery after a mid-run crash, no lost or duplicated work.
- **Rate limiting / backpressure** across all external calls.
- Resolve **local tool-calling validation**; set the routing rule accordingly.

**Adversarial / correctness test suite (new):**

- transactional outbox test; **duplicate-send chaos test**; **suppression-bypass test**;
- **cross-tenant isolation test**; **prompt-injection test suite**; CRM conflict-resolution tests;
- provider-webhook replay tests; hosted/local routing-fallback tests; PII-redaction tests;
- backup-restore test that verifies PII **and** vector-store deletion.

**Exit gate**

- The system runs unattended on a schedule for a sustained period without babysitting.
- Every adversarial test above passes (a suppressed contact cannot be sent to; a replayed webhook cannot double-send; one tenant cannot see another's data; an injected instruction in a prospect page is ignored).
- An injected scoring regression is caught by the eval harness.
- A forced crash recovers cleanly; a restore-from-backup is tested and verified.

---

## Phase 3 — Production Readiness (Real Sending at Volume, Real Client)

*Goal: everything required before a serious company relies on this and real cold mail goes out at volume.*

**Build**

- **Deliverability at scale:** dedicated authenticated domains (SPF/DKIM/DMARC), warmup, pacing, bounce/complaint handling, DMARC-report review, reputation monitoring.
- **Full compliance:** authoritative suppression before every send; permanent unsubscribe (incl. one-click / list-unsubscribe headers); retention/deletion; DSR; region-specific suppression behavior; multi-region rules (GDPR / CASL / CAN-SPAM — verify current).
- **Sending-provider terms approval** and a domain/mailbox ownership model; complaint and bounce **threshold policies** with automatic pausing.
- **Human-in-the-loop at scale:** batched review, confidence-based routing, edits-as-signal.
- **DR:** backups with tested restore; durability for in-flight state.
- **Audit trail**, **secrets rotation**, data-ownership/offboarding.
- **Client contract / DPA**, subprocessor list, **incident-response process**, abuse-prevention policy.
- **Full security and compliance review** of credential flows, injection controls, the send path, and tenant isolation.

**Exit gate**

- Real outbound sends at controlled volume with monitored deliverability (placement tracked, bounces/complaints handled, reputation stable, thresholds auto-pause).
- Every send checked against suppression; an unsubscribe permanently stops future contact across the correct scope.
- Restore-from-backup verified; the system passes a human security + compliance review.

---

## Phase 4 — Productization & Scale

*Goal: turn the single-client system into a product serving many clients efficiently.*

**Build**

- **Multi-tenancy productization** on the Phase-0 isolation primitives: strict per-client data/mailbox/sending isolation, self-serve where needed.
- **Self-serve onboarding & configuration** (profiles, mailboxes, CRM, domains, messaging) without hand-edited config.
- **Cost attribution & unit economics:** cost per client / campaign / booked meeting; per-tenant quotas and spend controls.
- **Concurrency & performance scaling:** address the single-GPU bottleneck (more hardware or shift the hot path to hosted).

**Exit gate**

- A new client is onboarded without hand-editing config.
- Two tenants run simultaneously with verified data and sending isolation.
- Per-client cost and profitability are visible.
- Target throughput sustained under concurrent multi-tenant load.

---

## Concern-to-Phase Map

| Concern | Phase |
| --- | --- |
| ICP/commercial thesis, source legality, sending-provider policy, jurisdiction matrix, controller/processor, n8n licensing, rough unit economics | Pre-build artifacts (gate real-data/real-send, not synthetic Phase 0) |
| Repo, tenant-aware schema, state machine, suppression/outbox/audit/source tables, routing seam, guardrail skeleton, logging, dry-run flag | 0 |
| Full pipeline (synthetic → real-data → tiny real-send), human gate + rubric, CRM sync, cost-per-meeting gate | 1A / 1B / 1C |
| Guardrail + adversarial tests, observability, evals, resumability, backpressure, local tool-calling validation | 2 |
| Deliverability at scale, full compliance, provider approval, thresholds, DR, audit, DPA, incident response, security review | 3 |
| Multi-tenancy productization, onboarding, cost attribution, concurrency scaling | 4 |
| Multi-tenancy *primitives* | 0 (built) |
| Multi-tenancy *decision* | Pre-build (decided) |

---

## What "MVP" means here, in one line

**A single-tenant, synthetic/seed run of the complete funnel (Phase 1A) — source, verify, score, personalize, approve, capture — on a tenant-aware schema with the state machine, suppression, send-eligibility gate, guardrail kill switches, structured logging, and one CRM sync all proven, and a cost-per-qualified-meeting projection that clears a threshold.** Real prospects (1B) and real sends (1C) come only after their legal and deliverability gates. Everything that makes it safe at scale (Phase 3) and sellable to many clients (Phase 4) builds on that proven core.

---

## Appendix — DIY / Zero-Cost Testing Stack

The free, open-source, self-hosted environment that Phases 0–2 are built and tested on. It is not a later add-on: these components are the ground the early phases run on, so they are stood up in Phase 0. The whole pipeline-proving effort runs on this stack for zero cost beyond model API tokens (and often not even that, when the local tier handles a task).

| Pipeline need | Paid/production option | Zero-cost testing substitute |
| --- | --- | --- |
| Lead / contact data | Lead-search + enrichment providers | **Synthetic leads** — Faker for structured fields; local model for realistic free-text (bios, "about us" copy, replies) |
| CRM (sync target) | HubSpot / Salesforce | **Self-hosted open-source CRM** — Twenty (GraphQL) or EspoCRM (REST), in Docker |
| Email sending | Reputable sending service + warmup | **Local mail catcher** — MailHog or Mailpit captures every "sent" mail; nothing leaves the machine |
| Orchestration, DB, queue, memory | Managed equivalents | n8n, Postgres, Redis, vector store — self-hosted |
| Observability | Managed monitoring | Self-hosted Grafana / Prometheus (or similar) |
| Reasoning / worker compute | Hosted tier | Local model tier (free); hosted tier only where routing requires it |

**On synthetic data (the key enabler):** synthetic leads are *better* than real data for testing because they are controllable — and they keep Phases 0–1A entirely clear of the personal-data obligations that begin the moment real prospects are ingested. Generate structured fields with Faker; use the local model for messier text. Deliberately inject edge cases: malformed emails, missing fields, an **injection attempt planted in a bio** (tests §11 controls), an ambiguous reply (tests triage). The local model both runs the pipeline and generates its own fixtures, for free.

**Where DIY has a hard ceiling (do not self-host past this):**

- **Deliverability for real cold mail.** DIY the send *path and logic* for free with a mail catcher, but real inbox placement depends on IP/domain reputation a from-scratch server cannot win. At real sending (Phase 1C/3), buy a domain and a reputable sending service.
- **Real contact data at prospecting volume.** Synthetic data proves the pipeline; real prospecting needs real people's real emails — and the legal gate — which is a Phase 1B/3 concern.

Both ceilings sit exactly at the synthetic → real-data → real-send boundary, which is where the roadmap already tells you to start spending (and complying) deliberately. Everything before that line is free.
