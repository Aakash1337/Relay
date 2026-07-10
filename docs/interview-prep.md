# RELAY interview prep — recruiter and deep technical Q&A

This is a practice guide for explaining RELAY in interviews. It is written in two styles:

1. **Recruiter screen questions** — concise, product/story-focused answers.
2. **Technical deep-dive questions** — interviewer-style prompts with strong answers that explain how the system actually works.

Use the answers as a baseline, then personalize them with concrete metrics, tradeoffs, and examples from your own work on the project.

---

## 1. Recruiter-screen questions

### Q1. Tell me about RELAY in one minute.

**Strong answer:**

RELAY is an autonomous B2B outbound system that takes a prospect from source registration through verification, scoring, personalization, human approval, send eligibility, sending, reply triage, and booking. The key idea is that unsafe sends should be structurally impossible, not merely discouraged by application code. So RELAY pushes critical invariants into PostgreSQL: tenant isolation is enforced with forced row-level security, state transitions are guarded by triggers, duplicate sends are prevented with unique constraints, and suppression rules are checked both before queueing and again at execution time. On top of that, the system has LLM routing seams, a guardrail harness, audit logs, SES sandbox sending, one-click unsubscribe, DSR erasure, retention purge, metrics, alerts, and a synthetic/adversarial test suite.

### Q2. What problem does RELAY solve?

**Strong answer:**

Outbound automation has two failure modes: it can be ineffective, or it can be dangerous. It can email people it should not email, bypass opt-outs, leak PII, over-send from a mailbox, duplicate messages, or silently switch providers and create unreviewed behavior. RELAY solves that by treating outbound as a stateful, auditable workflow with hard gates. The goal is not just to generate emails; it is to make the entire prospecting-to-booking funnel observable, tenant-isolated, compliant by construction, and safe enough to run unattended.

### Q3. What is technically impressive about it?

**Strong answer:**

The technically strongest part is the layered enforcement model. Many systems put compliance checks in service code only. RELAY duplicates critical checks across the API, domain services, workers, and database. For example, approval does not send; it only approves a specific draft version. The worker later claims a send job, rechecks suppression, lawfulness, dry-run flags, idempotency, real-send configuration, volume caps, and provider readiness, and the database trigger still rejects invalid send jobs even if someone tries raw SQL. That means the system is resilient to programmer mistakes, worker races, and partial failures.

### Q4. Why did you choose PostgreSQL as an enforcement layer instead of just using application logic?

**Strong answer:**

Because the highest-risk invariants are data invariants. Tenant boundaries, legal provenance, immutable state history, one-active-send constraints, and suppression checks need to hold regardless of which code path touches the database. PostgreSQL gives us row-level security, composite foreign keys, triggers, check constraints, partial unique indexes, advisory locks, and transactional semantics. That lets the database become the final authority. Application logic remains important for clear errors and orchestration, but the database is what prevents bypasses.

### Q5. What are the main safety guarantees?

**Strong answer:**

The major guarantees are:

- A tenant cannot read or mutate another tenant's rows through the app role.
- A lead cannot exist without registered source provenance and lawful-basis fields.
- A lead can only move through legal state-machine transitions.
- Approval never causes a send directly.
- Suppressed recipients cannot become send-eligible.
- Dry-run leads and campaigns cannot produce real sends.
- Real sends are disabled by default and require multiple attestations.
- Duplicate active sends are blocked by database uniqueness.
- Unsubscribes, complaints, and hard bounces create durable suppression entries.
- DSR erasure removes personal data but preserves a hashed do-not-contact marker.

### Q6. How would you explain the business value?

**Strong answer:**

The business value is controlled automation. Companies want outbound that scales, but they also need compliance, brand safety, deliverability protection, and tenant isolation. RELAY gives them an auditable funnel where human reviewers only inspect high-value moments, while the mechanical checks are automated and enforced. It also produces metrics like cost per booked meeting, spend headroom, bounce rates, edit rates, and queue health, so operators can improve campaigns safely rather than guessing.

### Q7. What stage is the project at?

**Strong answer:**

It is a prototype with the major roadmap phases implemented in code: foundations, synthetic dry-run MVP, real-data no-send pilot, reliability and observability, SES sandbox real-send pilot, production-readiness seams, and scale/productization features. The remaining work is mostly operational: production legal artifacts, KMS-managed secrets, provider/domain approvals, production runbooks, and real deployment hardening.

### Q8. What would you work on next?

**Strong answer:**

I would focus on production hardening. First, make the test environment one-command and CI-verifiable. Second, add a fail-fast production settings validator so production cannot boot with development secrets. Third, split the large API routes module into concern-specific routers. Fourth, add a security scanner baseline. Finally, run throughput benchmarks against target tenant and lead volumes to decide where parallelism, database indexes, or queue architecture need attention.

---

## 2. Architecture deep dive

### Q9. Walk me through the end-to-end lifecycle of a lead.

**Strong answer:**

A lead starts with a registered source and a campaign. The source records whether the source terms allow use, and the lead stores provenance fields such as lawful basis, source terms status, and region assumption. The pipeline then advances the lead through a formal state machine.

At a high level:

1. **Created** — the lead has been inserted with required provenance.
2. **Source/verification checks** — the pipeline confirms the lead is allowed to proceed.
3. **Scoring** — the compute layer scores fit, typically using deterministic offline behavior in tests or a configured hosted/local backend in deployment.
4. **Personalization** — the system drafts outreach copy using prompt scaffolding that marks prospect-authored content as untrusted data.
5. **Human approval** — a reviewer approves, edits, or rejects the exact draft version.
6. **Eligibility gate** — after approval, the system evaluates suppression, dry-run status, lawful basis, duplicate-send constraints, volume caps, provider config, unsubscribe mechanisms, and real-send attestations.
7. **Send queue** — eligible work becomes a send job, not an immediate send.
8. **Worker execution** — the send worker claims queued jobs, rechecks gates, and uses either the simulated sender or SES sender.
9. **Post-send events** — replies, bounces, complaints, deliveries, and unsubscribes update the lead or suppression tables.
10. **Booking/closure** — interested replies can move toward booking; terminal outcomes stop the sequence.

The critical design point is that each transition is explicit, auditable, and constrained by both code and database rules.

### Q10. Describe the system architecture in components.

**Strong answer:**

The main components are:

- **FastAPI API layer:** validates requests, resolves tenant identity from API keys, exposes tenant/admin/review/metrics/webhook endpoints, and never trusts request bodies to choose tenant context.
- **PostgreSQL datastore:** the canonical source of truth and enforcement layer for RLS, state transitions, suppression, idempotency, DSR, and audit history.
- **Pipeline runner:** orchestrates per-lead steps such as source checks, scoring, personalization, approval wait states, eligibility, reply triage, and booking.
- **Compute layer:** abstracts offline, OpenAI-compatible, Google, and Anthropic backends behind a task-routing seam.
- **Routing layer:** maps task types to local or hosted tiers, with rules like tool-calling never routes to local.
- **Guardrail harness:** enforces iteration and budget limits per run.
- **Senders:** simulated sender for dry-run and SES sender for the sandbox real-send pilot.
- **Workers:** send worker, SES event worker, and retention worker.
- **Ingestion:** SNS/SQS SES events and one-click unsubscribe flows.
- **Observability:** metrics, Prometheus output, alerts, ops UI, economics reports, and audit logs.
- **Synthetic/eval tooling:** deterministic prospect generation and golden-set evaluations.

### Q11. Why is approval separate from sending?

**Strong answer:**

Because combining approval and sending creates a high-risk one-click action. In RELAY, approval means, "this exact draft version is acceptable." It does not mean, "send now." After approval, the system still has to pass execution-time eligibility checks. This separation gives us several protections:

- A recipient might become suppressed after approval but before execution.
- A tenant might hit a daily cap after approval.
- Provider configuration might be disabled.
- A campaign or lead might be dry-run.
- A duplicate active send might already exist.
- A reviewer can approve content without being responsible for operational send timing.

The worker owns sending because sending is an operational act that needs fresh state and concurrency controls.

### Q12. How does tenant isolation work?

**Strong answer:**

Tenant isolation is enforced through multiple layers. The API derives the tenant ID from the tenant API key; request bodies do not get to choose it. Application code then opens a tenant-pinned session. A SQLAlchemy session event sets PostgreSQL's `app.tenant_id` transaction-local setting. PostgreSQL RLS policies compare each row's `tenant_id` against that setting. The app role is a non-superuser subject to forced RLS, so even accidental queries without a tenant context see an empty tenant-scoped world.

The schema also uses tenant-aware relationships and immutable `tenant_id` rules, so rows cannot be moved across tenants after creation. Admin operations use a separate schema-owner connection, and app code is not supposed to receive that engine except for migrations/bootstrap-style operations.

### Q13. Why separate admin and app database roles?

**Strong answer:**

The admin role owns schema and runs migrations/bootstrap operations. The app role runs API and worker code and is constrained by row-level security. This limits blast radius. If application code has a bug, it still operates as a role that cannot freely bypass tenant policies or delete arbitrary rows. The only intentional cross-tenant lookups are narrow `SECURITY DEFINER` functions, such as API-key-to-tenant resolution or queued-tenant discovery, and those should be reviewed carefully.

### Q14. How does the state machine work?

**Strong answer:**

The state machine defines legal lead transitions in Python and mirrors those rules into the database. Domain code calls a transition service that checks whether a move is legal, writes a transition row, and updates the lead. Database triggers backstop this so raw SQL cannot skip required states or jump from `created` directly to `sent`. Terminal states have no outgoing transitions, and error states are explicit so the system can distinguish retryable failures from terminal ones.

The purpose is to make lifecycle behavior inspectable. Instead of a lead having ambiguous booleans like `sent=true` or `booked=false`, it has a single state and an append-only transition history.

### Q15. How does the system prevent duplicate sends?

**Strong answer:**

Duplicate sends are prevented primarily with database constraints. Send jobs include tenant, campaign, lead, sequence step, and version/idempotency dimensions. The schema has uniqueness constraints so the same logical send cannot be queued twice, and partial uniqueness prevents more than one active send for a lead. The worker also claims jobs transactionally, using database locking patterns like `SKIP LOCKED`, so concurrent workers do not process the same job. Finally, provider execution is not retried blindly when the outcome is unknown; crash recovery treats mid-send ambiguity as fail-safe rather than sending again.

### Q16. How does suppression work?

**Strong answer:**

Suppression is a first-class domain concept. Suppression entries can apply at tenant, global, domain, campaign, or mailbox scope. Before a send can be queued or executed, the system checks whether the recipient is suppressed. Suppression entries are created manually, by unsubscribe, by hard bounce, by complaint, and by DSR erasure. For compliance signals like unsubscribe and hard bounce, the system decouples suppression from lead-state transitions: even if the lead is no longer in a state where it can transition to `unsubscribed` or `bounce_received`, the do-not-contact entry still lands.

Email addresses are stored as hashes rather than raw values in suppression rows. The current design uses HMAC-SHA256 with an email hash pepper, so a database dump alone is not enough to reverse common email addresses by dictionary attack.

### Q17. What happens when someone unsubscribes?

**Strong answer:**

For real sends, the SES sender can include List-Unsubscribe headers. If an HTTPS unsubscribe URL is configured, it embeds a signed per-job token that identifies tenant, lead, and send job without including PII. `GET /unsubscribe` renders a confirmation page and intentionally does not mutate state because mail clients and scanners may prefetch links. `POST /unsubscribe` verifies the token and processes the unsubscribe idempotently.

If the lead can legally transition to `unsubscribed`, the transition occurs and an auto-suppression trigger writes the suppression entry. If the lead is already terminal or in another state, the system still writes the suppression entry directly. That is the important guarantee: the opt-out is honored even when state-machine semantics prevent a neat transition.

### Q18. How does DSR erasure work?

**Strong answer:**

DSR erasure deletes personal data while preserving the do-not-contact guarantee. The flow resolves the target email to its hash candidates, writes a suppression entry first, then calls a database function that deletes rows carrying the person's data: leads, drafts, reviews, replies, send jobs, transitions, and related artifacts. The function is tenant-scoped and is the app role's only intended delete capability. CRM mirrors are removed too. The system leaves behind a hashed suppression marker so the person is not contacted again after erasure.

That is the core privacy tradeoff: delete PII, retain a non-PII-ish keyed digest needed for compliance with the opt-out.

### Q19. How does real sending work, and why is it safe?

**Strong answer:**

Real sending is off by default. The sender provider defaults to `none`, and `RELAY_REAL_SEND_ENABLED` must be true before real sends can happen. The Phase 1C real sender is SES sandbox direct sending. Even then, eligibility requires `test_consent`, a configured sender, pilot allowlist membership, identity and domain attestations, unsubscribe configuration, provider terms record, daily caps, and acceptable bounce/complaint windows. The SES sender does a final last-hop check that the lead email hashes to the send job's frozen recipient identity and that the recipient is in the pilot allowlist.

So a real send requires agreement from configuration, domain code, database constraints, worker execution checks, sender last-hop checks, and SES sandbox restrictions.

### Q20. Why use SES sandbox instead of going straight to a production provider?

**Strong answer:**

SES sandbox gives a safe direct-send pilot where AWS itself refuses non-verified recipients. That matches the project's safety posture: the first real sends are self-to-self and test-consent only. A production outbound provider or enrollment sender introduces more operational and compliance complexity: domain reputation, warmup, provider contracts, unsubscribe semantics, webhook fidelity, and user-visible risk. RELAY keeps the provider seam abstract but deliberately defers broader provider integration until the legal and deliverability gates are satisfied.

### Q21. How does SES event ingestion work?

**Strong answer:**

SES events arrive through SNS, either via HTTPS webhook or SQS polling. The system processes the raw SNS envelope only after signature verification. It pins signing certificate and subscription confirmation URLs to HTTPS SNS service hosts, then verifies the signature over the canonical SNS fields. After that, it parses the SES event.

Permanent bounces move matching sent leads to `bounce_received` and create hard-bounce suppression in the same transaction. If no lead is currently sent, the hard bounce still creates suppression. Complaints create complaint suppression entries idempotently. Deliveries are audit events. Unknown or malformed recipients are ignored without logging raw PII.

### Q22. How does the LLM/compute layer work?

**Strong answer:**

The compute layer is provider-agnostic. Each tier can use an offline deterministic backend, an OpenAI-compatible endpoint, Google Gemini/Gemma, or Anthropic Claude, selected by environment variables. The routing seam maps task types to tiers. Cheap bounded tasks can route local; tasks where mistakes cascade, like customer-facing outreach copy or orchestration, route hosted. Tool-calling is structurally disallowed on the local tier.

The important design principle is no silent fallback. If a configured real backend is unavailable or misconfigured, the system fails loudly instead of quietly using a different model and changing behavior without operator awareness.

### Q23. How do you defend against prompt injection?

**Strong answer:**

RELAY treats prospect-authored data as data, not instructions. Prompt scaffolding places untrusted prospect text into provenance-labeled escaped blocks, so a bio that says "ignore previous instructions" is represented as untrusted content. Synthetic data includes adversarial examples like hostile bios and injection-like replies. The tests and evals assert that injection attempts cannot raise scores, manufacture buying intent, or bypass gates. For reply triage, hostile or opt-out-like content should move toward less contact, not more.

This is not a claim that prompt injection is solved universally; it is a layered mitigation. The model output is constrained by downstream state machine, approval, eligibility, suppression, and audit rules.

### Q24. What is the guardrail harness?

**Strong answer:**

The guardrail harness enforces dumb, reliable limits around intelligent behavior. It tracks per-run iteration count and budget units. If a run exceeds the iteration cap, it is killed. If it exceeds budget, it is killed. Those kills are persisted to pipeline run records, so they are visible operationally. The point is that when the intelligent component behaves badly, the safety mechanism should not depend on intelligence; it should be a simple counter or budget check.

### Q25. How do retries and crash recovery work?

**Strong answer:**

Transient compute failures are retried with bounded exponential backoff, but refusals and invalid outputs are not retried because retrying those can re-roll a decision. If retries are exhausted, the lead parks in an explicit error state. Later pipeline runs can resume retryable states without duplicating completed work.

Crash recovery runs on worker ticks. It closes stale pipeline runs and handles orphaned mid-send jobs. For sends, the conservative policy is important: if a worker crashed after handing work to a provider and the outcome is unknown, the system does not blindly retry and risk a duplicate. It fails safe and requires later reconciliation or operator handling.

### Q26. How does rate limiting and backpressure work?

**Strong answer:**

External calls pass through token buckets by target: local compute, hosted compute, and CRM. If a token is available, the call proceeds. If not, the limiter calculates the wait. If the wait exceeds the configured maximum, it raises backpressure rather than sleeping indefinitely or building an unbounded queue. The caller can then park work visibly. This is better than silent throttling because operators can see pressure and scale or tune configuration.

### Q27. How does observability work?

**Strong answer:**

Observability is derived from the rows the system already writes. Metrics endpoints expose funnel counts, queue state, spend, reputation signals, suppression counts, bounce/complaint rates, and edit rates. Prometheus text export supports standard scraping. Alerts cover failure streaks, spend spikes, stuck queues, and reputation thresholds. There are self-contained review/admin/ops pages that do not introduce a frontend build pipeline.

The design favors auditable facts over separate analytics events. If the pipeline writes transition, run, send, and review rows correctly, metrics can be computed from those facts.

### Q28. How do economics and spend controls work?

**Strong answer:**

Each pipeline run tracks cost units through the guardrail harness. Economics reports aggregate funnel counts and cost units at campaign or tenant level. If `RELAY_COST_UNIT_USD` is calibrated, reports can project USD. Tenants can have rolling monthly spend caps. At or above the cap, new pipeline runs are refused with a recorded kill, while in-flight runs continue under their own budgets. Alerts can warn at 80% and fire critically at 100%.

### Q29. How does human review work at scale?

**Strong answer:**

Drafts enter a pending review queue. Reviewers can approve, approve with edits, or reject with a controlled reason. Approve-with-edits creates or supersedes the approved content so the exact human-approved version is what can later send. Review decisions are append-only in `draft_reviews`.

At scale, the queue is confidence-ordered and batch review can process many decisions. Each item is handled independently so one stale or invalid review does not fail an entire batch. Edit rate is tracked as a signal: if reviewers frequently edit generated copy, prompts or targeting may need improvement.

### Q30. How do multi-step sequences work?

**Strong answer:**

Campaigns can define sequence length and delay. After a sent step receives no terminal signal, the next step can re-enter the pipeline after the configured delay. Each step creates its own draft version and requires its own approval and eligibility checks. Replies, bounces, unsubscribes, and suppression cancel remaining steps structurally. This avoids the common automation bug where a follow-up continues after a recipient opted out or replied.

---

## 3. Database and security deep dive

### Q31. What database features does RELAY rely on most?

**Strong answer:**

The most important PostgreSQL features are:

- Forced row-level security for tenant isolation.
- Transaction-local settings for tenant context.
- Triggers for state-machine and insert/update guards.
- Composite foreign keys and immutable tenant IDs.
- Partial unique indexes for one-active-send constraints.
- `SECURITY DEFINER` functions for narrow cross-tenant operations.
- Advisory locks for race-proof send caps.
- Transactional DSR and suppression behavior.
- `SKIP LOCKED`-style worker claims for concurrent processing.

This is why tests run against real PostgreSQL instead of SQLite or mocks.

### Q32. What is the biggest security risk in the current prototype?

**Strong answer:**

The biggest current risk is operational hardening rather than a missing conceptual gate. For example, dev cryptographic defaults are documented as not-for-production, but production should fail fast if those defaults are still present. Similarly, the project should have a repeatable test environment, a security scanner baseline, and clear runbooks. The architecture already has strong safety layers, but production safety depends on making the environment and deployment posture just as rigorous.

### Q33. How are API keys stored and rotated?

**Strong answer:**

Tenant API keys are generated as high-entropy `rk_...` tokens and only their SHA-256 hashes are stored. Authentication hashes the presented key and uses a narrow database function to resolve the tenant. Rotation is admin-triggered: a new key is generated, the stored hash is replaced, the old key immediately stops working, and an audit record is written. There is no grace overlap because rotation is treated as a suspected-exposure response.

### Q34. Why are email hashes peppered but API-key hashes are not?

**Strong answer:**

Email addresses are guessable. If you store unsalted SHA-256 hashes of emails, an attacker with a common email list can reverse many rows offline. So RELAY uses HMAC-SHA256 with a long-lived email hash pepper. API keys, by contrast, are high-entropy random secrets. A plain SHA-256 hash of a sufficiently random API key is acceptable because dictionary attacks are not practical in the same way. The design keeps the email pepper stable because rotating it casually would break lookups for all stored email digests.

### Q35. How does the system avoid logging PII?

**Strong answer:**

The logging layer redacts PII-like values, and domain code generally logs recipient hashes rather than raw emails. SES event handling hashes recipient addresses immediately and avoids logging malformed raw recipient values. Audit payloads are designed to contain IDs, hashes, actions, and metadata rather than raw personal text. The test suite includes PII log/audit checks to pin this behavior.

### Q36. How does the unsubscribe token avoid exposing PII?

**Strong answer:**

The token contains version, tenant ID, lead ID, send job ID, and an HMAC signature. It does not contain the recipient email. The signing key is derived per tenant from the master key and an unsubscribe purpose string. Verification accepts the current master key and optionally a previous master key during rotation, so old unsubscribe links keep working. If someone tampers with tenant, lead, or job IDs, signature verification fails.

### Q37. How would you threat-model a bad send?

**Strong answer:**

I would ask what has to fail simultaneously for a bad send to leave the system:

1. The lead/campaign dry-run flags must permit real sending.
2. Legal provenance and preflight must permit the lead.
3. The recipient must not be suppressed.
4. A human must approve the exact draft.
5. Eligibility must pass at queue time.
6. Database triggers must allow the send job.
7. Worker execution must recheck eligibility.
8. Concurrency controls must not be bypassed.
9. Sender last-hop recipient-hash check must pass.
10. Pilot allowlist and provider sandbox must permit delivery.

That is a layered defense. The goal is not one perfect check; it is making a dangerous outcome require multiple independent failures.

### Q38. What is the most subtle race condition you had to design around?

**Strong answer:**

Send caps and duplicate sends are subtle because multiple workers can race. A naive implementation might check daily count, see room, and both workers send. RELAY addresses this with transaction-level claims, uniqueness constraints, and per-tenant serialization for cap-sensitive checks. Jobs are claimed with locking so two workers do not own the same job, and cap evaluation happens under a per-tenant advisory lock so racing workers cannot both pass a boundary condition.

### Q39. What happens if a recipient is suppressed after a job is queued but before the worker runs?

**Strong answer:**

The worker rechecks eligibility at execution time. A queued job is not a guarantee of sending; it is work pending a final check. If suppression appears after queueing, execution should block or defer according to the reason, and the lead should not be sent. This is one of the main reasons approval and queueing are separated from execution.

### Q40. What happens if the provider reports a hard bounce for a lead that is no longer in `sent`?

**Strong answer:**

The system still suppresses the address. If the matching lead is currently `sent`, the bounce transition happens and auto-suppression writes the suppression entry. If no matching lead is in `sent`, perhaps due to replay or because the lead already moved terminal, the handler directly creates the hard-bounce suppression entry unless it already exists. The compliance signal is not dropped just because the state transition is no longer available.

---

## 4. Reliability, testing, and operations

### Q41. Why does the test suite require real PostgreSQL?

**Strong answer:**

Because the core guarantees rely on PostgreSQL-specific behavior: RLS, triggers, advisory locks, partial indexes, composite constraints, transaction semantics, and security-definer functions. SQLite or mocks could test service code but would not test the enforcement layer. Since the database is part of the product's safety boundary, tests need to run against the real database engine.

### Q42. What are the most important tests?

**Strong answer:**

The most important tests are the exit-gate and adversarial tests:

- Full fake-lead journey through the state machine.
- Infinite-loop and budget guardrail kills.
- Reprocessing closed leads as no-ops.
- Duplicate send rejection.
- Dry-run cannot send from multiple attack angles.
- Cross-tenant reads and transitions fail.
- Suppressed recipients cannot become send-eligible.
- PII stays out of logs and audit payloads.
- No lead enters without source provenance.
- Race conditions around duplicate sends and send caps.
- Webhook replay/idempotency behavior.
- DSR erasure and backup/restore expectations.

Those tests represent product promises, not just implementation details.

### Q43. What did your recent audit find?

**Strong answer:**

The audit found that the project has strong architectural safety boundaries, but needs operational hardening. Ruff passed. Pytest could not run in the audit environment because the expected PostgreSQL test service was unavailable. Pylint produced a mix of real maintainability warnings and expected SQLAlchemy false positives. Bandit was not installed. The main recommendations were: make tests one-command reproducible, add production secret validation, triage Pylint, add security scanning, and split the large API routes module.

### Q44. How would you make local development smoother?

**Strong answer:**

I would make `just test` or a new `just verify` command bring up the test database automatically or fail with a short actionable message. I would also add a preflight check that verifies PostgreSQL is reachable before running the full suite, because hundreds of repeated connection errors obscure the root cause. For linting, I would separate mandatory checks from advisory checks so contributors know what blocks a PR.

### Q45. How would you deploy RELAY?

**Strong answer:**

I would deploy the API and workers separately but against the same PostgreSQL cluster. The API handles tenant/admin/review/webhook endpoints. Send workers process queued jobs. Event workers poll SQS if webhook ingestion is not used. Retention workers run on a schedule. Secrets would come from a managed secrets system, not `.env`. PostgreSQL would have migrations run by the admin role, while runtime services use the app role. Metrics would be scraped by Prometheus, alerts forwarded to an incident channel, and SES/SNS/SQS configured with least-privilege IAM.

### Q46. What runbooks would production need?

**Strong answer:**

Production needs runbooks for:

- Tenant onboarding and API key rotation.
- Sender identity verification and attestation.
- SES/SNS/SQS webhook setup and replay handling.
- Bounce-rate or complaint-rate incidents.
- Suppression import/export and global suppression.
- DSR erasure verification.
- Retention purge verification.
- Worker crash recovery and orphaned send jobs.
- Master-key rotation with unsubscribe-token compatibility.
- Database migration rollback or forward-fix strategy.
- Deliverability warmup and cap tuning.

### Q47. What would you monitor first?

**Strong answer:**

I would monitor queue age, send job status distribution, failure streaks, retryable error counts, terminal error counts, daily sends per tenant/mailbox, bounce and complaint rates, unsubscribe rate, suppression counts by reason, spend units per hour, monthly spend cap headroom, review edit rate, and worker throughput. These metrics show whether the system is safe, healthy, and economically viable.

### Q48. How would you scale it?

**Strong answer:**

First I would measure throughput with the benchmark script rather than guessing. Then I would scale workers horizontally while relying on `SKIP LOCKED` claims and per-tenant advisory locks. I would add or tune indexes around queue scans, tenant/time filters, suppression lookups, and metrics queries. For very high volume, I would consider partitioning time-series tables like audit logs and pipeline runs, and possibly moving some scheduling to a durable queue. But I would keep PostgreSQL as the invariant authority unless proven otherwise.

---

## 5. Design tradeoffs and critique

### Q49. What is a design tradeoff you made?

**Strong answer:**

A major tradeoff is duplicating checks across application code and database triggers. That adds complexity and requires careful test coverage to keep behavior consistent. But for this domain, the cost is worth it because the consequences of a bad send, cross-tenant leak, or ignored unsubscribe are serious. Application checks provide clear control flow and user-friendly errors; database checks provide non-bypassable safety.

### Q50. What is something you would refactor?

**Strong answer:**

I would split the large API routes file into multiple routers by concern: tenant onboarding/admin, lead/campaign management, review, metrics/economics, webhooks/unsubscribe, DSR/preflight, and internal worker ticks. The current file is understandable but too broad. Splitting it would make auth boundaries more obvious and reduce review risk when adding endpoints.

### Q51. What is something you deliberately did not build?

**Strong answer:**

I deliberately did not open production real-prospect sending. The project supports SES sandbox test-consent sends, but real-prospect production sending requires legal artifacts, provider approvals, domain setup, deliverability posture, KMS secrets, and operational runbooks. The code has seams for this, but the responsible choice is to keep it gated until those non-code prerequisites are satisfied.

### Q52. Where could the design be over-engineered?

**Strong answer:**

For a small prototype, using PostgreSQL RLS, triggers, multi-role database access, worker orchestration, and extensive audit trails is heavier than a simple CRUD app. But RELAY is not a simple CRUD app; it automates communication with real people. The over-engineering risk is real, but the safety requirements justify many of these choices. The key is to keep developer ergonomics good with migrations, fixtures, docs, and clear tests.

### Q53. What is the biggest limitation of using process-local token buckets?

**Strong answer:**

Process-local buckets only limit within one process. If you run multiple worker processes or pods, each has its own bucket, so aggregate throughput may exceed the intended global rate. For early phases this is acceptable and documented, but production distributed rate limiting should use a shared backend such as Redis, PostgreSQL advisory mechanisms, or provider-side quotas depending on the target.

### Q54. How would you handle model quality regressions?

**Strong answer:**

I would use the eval harness and golden-set invariants as a gate before changing providers, models, or prompts. Important invariants include opt-outs triaging to unsubscribe, hostile prompts not increasing scores, generated copy staying within length and content bounds, and customer-facing output using the right tier. I would also track reviewer edit rate in production as a live signal that copy quality or prompt alignment is drifting.

### Q55. What would you say if an interviewer challenges whether LLMs belong in this system?

**Strong answer:**

LLMs are useful here for enrichment, scoring, summarization, personalization, and reply triage, but they are not trusted as policy enforcers. RELAY treats LLM output as one input into a constrained workflow. Legal gates, suppression, idempotency, dry-run status, tenant isolation, approval, and send eligibility are deterministic and database-backed. So the architecture uses LLMs where they add leverage, while keeping irreversible or compliance-sensitive decisions behind deterministic controls.

### Q56. How do you know the system is safe unattended?

**Strong answer:**

Unattended safety comes from convergence and idempotency. The pipeline has explicit wait states for humans and workers; reprocessing completed or terminal leads is a no-op; duplicate jobs are rejected; workers claim jobs transactionally; recovery closes orphans; and further scheduled ticks should not change a converged cohort. The unattended tests exercise a simulated schedule to prove repeated ticks converge rather than creating duplicate actions.

---

## 6. Behavioral interview prompts using RELAY

### Q57. Tell me about a time you chose safety over speed.

**Strong answer:**

In RELAY, I chose to keep real sending structurally closed until legal and deliverability gates existed. It would have been faster to add a direct send endpoint after generating drafts, but that would have combined approval, eligibility, and sending into one risky path. Instead, I implemented a separate approval gate, send queue, worker rechecks, database triggers, dry-run enforcement, pilot allowlist, and SES sandbox. That slowed the path to sending, but it made the system safer and easier to reason about.

### Q58. Tell me about a difficult technical decision.

**Strong answer:**

A difficult decision was whether to put state-machine and compliance enforcement in the application only or duplicate it in PostgreSQL. Application-only enforcement is simpler and faster to build, but it is easier to bypass accidentally. I chose database-backed enforcement because tenant isolation, suppression, and send eligibility are core invariants. The tradeoff was more complex migrations and tests, but the benefit was much stronger guarantees.

### Q59. Tell me about handling ambiguity.

**Strong answer:**

The project had ambiguity around real sending. Instead of pretending code alone could solve legal and deliverability readiness, I separated what code can enforce from what requires human/operator attestation. The system records preflight approvals, sender identity attestations, provider terms records, and configuration gates. That let the code enforce the presence and freshness of decisions without hardcoding legal judgment into the application.

### Q60. Tell me about a failure mode you designed for.

**Strong answer:**

I designed for worker crashes during send execution. If a worker crashes before sending, retrying is safe. But if it crashes after handing a message to the provider and before recording the result, retrying could duplicate an email. RELAY treats unknown send outcomes conservatively: it does not blindly retry mid-send orphans. It fails safe and records the issue for recovery or operator handling.

### Q61. Tell me about improving maintainability.

**Strong answer:**

The project uses explicit seams: compute backends, sender providers, CRM adapters, routing policy, workers, and domain services. Those seams let behavior change through configuration or isolated adapters rather than large rewrites. At the same time, the audit identified maintainability debt in the API routes module and Pylint configuration. My next step would be to split routers and clean static-analysis signals so future changes are easier to review.

---

## 7. Rapid-fire interviewer questions

### Q62. Why not just use a SaaS outbound platform?

**Answer:** Because RELAY is about owning the safety and orchestration layer: tenant isolation, compliance gates, explainable state transitions, model routing, and auditability. A SaaS provider may still be useful behind the sender seam later, but RELAY keeps policy and state under our control.

### Q63. Why no silent model fallback?

**Answer:** Silent fallback changes behavior without operator consent. If Claude is configured and fails, silently using a local model could alter quality, safety, and cost. RELAY fails loudly so operators know the configured reasoning path is broken.

### Q64. Why include synthetic adversarial data?

**Answer:** Because prompt-injection and edge-case handling should be tested before real data enters. Synthetic data lets the project repeatedly test hostile bios, sparse records, unicode names, oversized fields, opt-outs, and weird replies without PII.

### Q65. Why use append-only audit logs?

**Answer:** The system needs to answer who approved what, why a lead moved states, when a key rotated, why a suppression was added, and what worker action occurred. Append-only logs preserve accountability and incident reconstruction.

### Q66. What is the difference between a retryable and terminal error?

**Answer:** Retryable errors are transient infrastructure/provider failures where rerunning the same step is safe. Terminal errors are refusals, invalid outputs, policy violations, or exhausted retries where rerunning would re-roll a decision or hide a real issue.

### Q67. How do you handle a CRM outage?

**Answer:** CRM sync is best-effort and one-way. It is not on the send path. A CRM outage can be logged or retried, but it cannot approve, block, or send outreach.

### Q68. Why is GET unsubscribe non-mutating?

**Answer:** Mail clients and security scanners prefetch links. If GET mutated state, scanners could accidentally unsubscribe users. Only POST honors the unsubscribe.

### Q69. How is a global suppression different from tenant suppression?

**Answer:** Tenant suppression blocks one tenant from contacting an address. Global suppression blocks every tenant. Because global suppression has broader blast radius, it is an admin action rather than a normal tenant action.

### Q70. What does "structurally impossible" mean in this project?

**Answer:** It means the system is designed so invalid actions are rejected by durable constraints and gates, not just avoided by convention. For example, a suppressed recipient cannot become send-eligible even if a developer forgets an application-level check, because the database and worker rechecks still block it.

---

## 8. Questions you can ask the interviewer

These questions show senior-level thinking and connect RELAY to production concerns:

1. How does your team decide which product invariants belong in application code versus the database?
2. What compliance or privacy workflows are hardest to test in your current systems?
3. How do you handle worker crashes around non-idempotent external calls?
4. Do you prefer advisory static-analysis gates first, or strict gates from day one?
5. How do you monitor model-quality regressions after prompt or provider changes?
6. What is your team's approach to tenant isolation in multi-tenant SaaS?
7. How do you evaluate whether automation is safe enough to run unattended?
8. What runbooks do you expect before a system can send messages to real customers or prospects?
9. How do you balance reviewer throughput with human accountability in approval workflows?
10. What would you want to see in RELAY before trusting it in production?
