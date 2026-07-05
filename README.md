# RELAY

**Autonomous B2B sales prospecting and outreach. Outbound that compounds.**

RELAY automates the prospecting-to-booking funnel as an orchestrated,
observable pipeline: source → verify → score → personalize → **human
approval gate** → **send-eligibility gate** → send → reply triage →
booking. It is designed so that an unlawful, suppressed, or duplicate
send is *structurally impossible* — enforced by the database, not by
good intentions.

This repository currently implements **Phase 0 — Foundations &
Scaffolding**, **Phase 1A — Synthetic dry-run MVP**, **Phase 1B —
Real-data, no-send pilot**, **Phase 2 — Reliability, observability &
evaluation**, and **Phase 1C — Tiny real-send pilot** (SES sandbox,
self-to-self) of the
[development roadmap](RELAY-development-roadmap.md) (on the `Plan`
branch, together with the full
[project documentation](RELAY-project-documentation.md)): the pipeline
runs end to end with real reasoning seams; real-person data is gated
behind a recorded Legal/Data Preflight, and the send path stays
structurally closed for it until Phase 1C.

---

## What exists after Phase 0

| Foundation | Where |
| --- | --- |
| Tenant-aware schema — `tenant_id` on every table, FORCEd Postgres RLS, composite FKs, immutable `tenant_id` | `src/relay/db/models.py`, `src/relay/db/sql/004_rls.sql` |
| The formal lead state machine (§4), enforced in code **and** DB trigger, seeded from one Python map | `src/relay/domain/states.py`, `state_machine.py`, `sql/002_functions.sql` |
| Core tables: leads, suppression, outbox/send-jobs, append-only audit log, lead source register, pipeline runs | `src/relay/db/models.py` |
| §7 hard rule: no lead without a registered source whose terms allow use + full provenance fields (NOT NULL) | `fn_lead_insert_guard`, CHECK constraints |
| Suppression contract (§10) with tenant/global/domain/campaign/mailbox scopes; auto-suppress on unsubscribe & hard bounce | `src/relay/domain/suppression.py`, `fn_is_suppressed`, `fn_auto_suppress` |
| Split send path: approval **never** sends; internal worker re-checks every invariant at execution time | `src/relay/domain/approval.py`, `src/relay/workers/send_worker.py` |
| Send-eligibility gate (§10 checklist) in code + DB trigger re-check on queue **and** claim | `src/relay/domain/eligibility.py`, `fn_send_jobs_guard` |
| Idempotency as a DB UNIQUE constraint (`tenant, campaign, lead, step, version`) + one-active-send partial index | `send_jobs` table |
| Guardrail harness: max-iteration counter, per-run budget ceiling, kills persisted to `pipeline_runs` | `src/relay/guardrails/harness.py` |
| Task-routing seam (§8), stubbed: local vs hosted per task type; tool-calling never routes local | `src/relay/routing/` |
| Structured JSON logging with in-process PII redaction (emails → suppression-compatible hashes) | `src/relay/logs.py` |
| Hard `dry_run` flag on leads **and** campaigns — immutable, DB-enforced; in Phase 0 no real sender could even be constructed (one exists since 1C, strictly behind the §6 gates) | `src/relay/senders/`, `fn_send_jobs_guard` |
| Backend API skeleton + health check; n8n spine workflow calling it | `src/relay/api/`, `infra/n8n/relay-spine.json` |
| Tenancy primitives: per-tenant key derivation, vector-store namespaces | `src/relay/tenancy.py`, `src/relay/hashing.py` |

**What Phase 0 deliberately does NOT contain:** real prospect data (gated
by the Legal/Data Preflight, Phase 1B), real sending (gated by
deliverability + provider approval, Phase 1C), any model calls (the
reasoning stubs land in Phase 1A), CRM sync, and the approval UI.

---

## What Phase 1A adds

| Capability | Where |
| --- | --- |
| Compute backends behind the routing seam — each tier picks a provider + model in `.env`, independently: `offline` (deterministic, hermetic — the default), `openai`-compatible (Ollama/vLLM), `google` (Gemini API: Gemini *and* Gemma models, thinking-part aware), `anthropic` (Claude API, adaptive thinking). Swapping the orchestrator or workhorse model is config, never code | `src/relay/compute/` |
| §11 prompt scaffolding: every piece of prospect-authored text enters prompts entity-escaped inside a provenance-labeled `<untrusted_data>` block; a bio saying "ignore previous instructions" is data, not instruction | `src/relay/compute/prompting.py` |
| No silent fallback: backends are chosen by `RELAY_COMPUTE_*` config only; a misconfigured real backend fails loudly instead of quietly degrading | `src/relay/compute/registry.py` |
| Synthetic prospects (Faker, seeded, all at `.test` domains) with documented edge cases: prompt-injection bios, unicode names, oversized bios, sparse records, plus-addressing | `src/relay/synthetic/` |
| Real pipeline data flow: enrichment/scoring/personalization consume the lead's fields; scoring branches on `RELAY_FIT_SCORE_THRESHOLD`; triage runs on the actual reply body and records a write-once outcome on the `replies` row | `src/relay/pipeline/runner.py` |
| Replies as first-class rows: tied to a concrete send job, content trigger-frozen, triage write-once; simulated in 1A, webhook-shaped for 1C | `replies` table |
| Reviewer rubric at the human gate: approve / approve-with-edits / reject with a controlled reason vocabulary, recorded append-only in `draft_reviews`; approve-with-edits supersedes the draft with the human's text and approves *that* version | `src/relay/domain/approval.py` |
| Minimal approval UI: one self-contained HTML page at `/review` (queue → draft → decision), no build step, no external assets; the page itself says approval never sends | `src/relay/api/review_ui.py` |
| Economics gate: funnel counts + cost-per-booked-meeting derived from rows the pipeline already writes; USD projection only when `RELAY_COST_UNIT_USD` is calibrated | `src/relay/economics.py`, `GET /campaigns/{id}/economics` |
| CRM sync seam: one-way best-effort mirror (InMemory for dev, EspoCRM adapter), never on the send path — a CRM outage cannot touch a gate | `src/relay/crm/` |

Injection-hostile inputs are part of the synthetic corpus on purpose:
the exit tests assert a hostile bio changes nothing about the gates and
a hostile reply can only ever move triage toward *less* contact
(`unsubscribed`), never more.

---

## What Phase 1B adds

Real people's data may now enter — behind a recorded gate, with a full
exit door.

| Capability | Where |
| --- | --- |
| **Legal/Data Preflight gate**: a lead whose `lawful_basis` is anything but `synthetic`/`test_consent` is rejected at INSERT (DB trigger) unless the tenant has an approved, unrevoked preflight record pinned to the artifact's SHA-256. Approve/revoke/status via admin endpoints | `docs/legal-data-preflight.md` (template), `data_preflight` table, `/internal/preflight/*` |
| **Real-data ingestion rules**: real-basis leads must carry `retention_until` and a real deliverable domain (reserved/test TLDs rejected); enforced at the API boundary (422) *and* in the trigger (raw SQL included) | `fn_lead_insert_guard`, `LeadCreateRequest` |
| **Send path closed for real people**: a real-basis lead flows source → score → draft → human gate, but no send job can exist for it in ANY mode — blocked at the eligibility gate and again by `fn_send_jobs_guard`. Phase 1C opens this behind its own gates | `eligibility.py`, `fn_send_jobs_guard` |
| **DSR erasure** (right to be forgotten): `POST /dsr/erasure` deletes every row carrying the person's data (lead, drafts, reviews, replies, send jobs, transitions) via `fn_dsr_erase` — the app role's *only* delete capability, tenant-guarded in SQL — removes the CRM mirror, and leaves exactly one thing behind: a hashed do-not-contact suppression entry. Suppression-first, same transaction | `src/relay/domain/dsr.py`, `fn_dsr_erase` |
| **Retention purge**: `just retention` / `relay-retention` deletes leads past `retention_until` through the same audited path — without fabricating a suppression entry (expiry is not an opt-out) | `src/relay/workers/retention_worker.py` |

What Phase 1B still does NOT contain, by design: any real sending
(Phase 1C: deliverability, provider approval, volume caps) — and the
preflight *content* itself, which is a human/legal deliverable the
system only records and enforces.

---

## What Phase 1C adds

The first real emails — SES **sandbox**, self-to-self only, per the §6
[sending-provider decision record](docs/decisions/sending-provider.md).

| Capability | Where |
| --- | --- |
| **Provider seam** with the two operational shapes from §6: `DirectSender` (SES, implemented) and `EnrollmentSender` (Smartlead — interface + idempotency-boundary contract only; the adapter is deliberately deferred to real-prospect production). Config-selected via `RELAY_SENDER_PROVIDER`; default `none` keeps real sending structurally absent | `src/relay/senders/` |
| **SES adapter**: SESv2 direct send with List-Unsubscribe/One-Click headers and a last-hop cross-check that the recipient hashes to the job's frozen identity | `senders/ses.py` |
| **Real-mode eligibility, for real**: on top of the seven always-on integrity checks, real mode adds ten more — master switch, `test_consent` basis, pilot allowlist, sender configured, identity attest, domain-auth attest, daily volume cap (race-proof via a per-tenant advisory lock), bounce/complaint reputation window, unsubscribe target, §6 record reference. Real sends are `test_consent`-only (our own inboxes), in code AND in the send-jobs trigger | `domain/eligibility.py`, `fn_send_jobs_guard` |
| **SES event ingestion**: SNS envelopes, signature-verified against the AWS signing cert, via HTTPS webhook (`/webhooks/ses?token=…`) or SQS polling (`just events`) — hard bounces transition the lead and auto-suppress in one transaction; complaints suppress once; everything idempotent under provider redelivery | `src/relay/ingest/`, `workers/event_worker.py` |
| **Live smoke checklist** for when AWS credentials/DNS land | `docs/phase1c-live-smoke.md` |

---

## What Phase 2 adds

Make the proven pipeline trustworthy unattended, and measurable enough
to change safely.

| Capability | Where |
| --- | --- |
| **Resumability**: transient compute failures park leads in `error_retryable` (the failed step's transaction already rolled back — no partial work) and a later run resumes them; the DB trigger counts retries and enforces the cap; refusals park terminally for a human | `pipeline/runner.py` |
| **Crash recovery on every tick**: orphaned runs get closed, orphaned mid-send jobs fail safe (outcome unknown ⇒ never retried, never assumed sent); idempotent, wired into the worker tick | `pipeline/recovery.py` |
| **Rate limiting & backpressure**: token buckets per external target (compute tiers, CRM); waits beyond the cap raise visibly and park work; bounded exponential retry for *transient* failures only — refusals are never re-rolled and providers are never silently swapped | `ratelimit.py` |
| **Observability**: `/metrics` (JSON) + `/metrics/prometheus`, all derived on read from rows the pipeline already writes; `/ops` self-contained dashboard; alert rules (spend spike, failure streak, stuck queue) with log + webhook sinks | `observability/`, `api/ops_ui.py` |
| **Eval harness**: golden-set invariants through the real prompt scaffolding — opt-outs must triage `unsubscribed`, injections cannot raise scores or manufacture intent, copy respects bounds; `just evals` scores the *configured* backends; an injected regression is provably caught | `evals/`, `scripts/run_evals.py` |
| **Adversarial suite**: duplicate-send chaos (racing workers), outbox atomicity, suppression bypass from every angle, webhook replays, CRM conflicts, cross-tenant erasure attempts, PII log sweep, and a pg_dump→restore test proving erasure survives backups | `tests/test_adversarial.py` |
| **Unattended-run proof**: a simulated spine schedule converges a mixed cohort and further ticks change nothing | `tests/test_unattended.py` |
| **§8 open item resolved**: the local tier stays structurally tool-free, with revisit criteria recorded | `docs/decisions/local-tool-calling.md` |

---

## Phase 3 — production readiness (in progress)

| Capability | Where |
| --- | --- |
| **One-click unsubscribe (RFC 8058)**: every real send embeds a per-job signed-token URL in its List-Unsubscribe header (beside the mailto). `GET /unsubscribe` renders a confirm page and never mutates state (mail clients and scanners prefetch links); the `POST` honors it idempotently — the lead transitions to `unsubscribed` where the state machine allows, and the do-not-contact suppression entry ALWAYS lands, decoupled, same pattern as bounces. Tokens are HMAC-signed with a per-tenant derived key and carry no PII | `ingest/unsubscribe.py`, `api/routes.py`, `senders/ses.py` |
| **Deliverability pacing**: per-mailbox rolling-hour cap, minimum spacing between sends, and a warmup ramp that grows the effective daily cap from the tenant's first real send (`min(cap, start + increment·day)`). Pacing is execution-time only and **defers** — a paced-out job stays queued for a later tick, its lead untouched; it is never terminally blocked over a temporal condition. Evaluated under the same per-tenant advisory lock as the daily cap, so racing workers cannot both pass at a pace boundary. All off by default (`RELAY_REAL_SEND_HOURLY_CAP`, `RELAY_REAL_SEND_MIN_SPACING_SECONDS`, `RELAY_WARMUP_DAILY_*`) | `domain/eligibility.py`, `workers/send_worker.py` |
| **Human-in-the-loop at scale**: the review queue is confidence-ordered (highest `fit_score` first — the batchable tail on top, reviewer attention at the bottom); a batch-review endpoint processes up to 100 rubric decisions per call, each in its own transaction so one stale item fails alone; the edit rate (`approved_with_edits` share) is a first-class metric — edits-as-signal for prompt iteration | `api/routes.py`, `observability/metrics.py` |
| **Reputation monitoring**: 24h bounce/complaint rates and per-reason suppression counts in `/metrics` and the Prometheus export; a `bounce_rate_high` critical alert fires past `RELAY_ALERT_BOUNCE_RATE` — with a `_MIN_SENDS` floor so 1-of-1 noise never pages — BEFORE the eligibility threshold silently pauses sending | `observability/metrics.py`, `observability/alerts.py` |
| **Secrets rotation**: `POST /internal/tenants/{id}/rotate-key` (admin) issues a new tenant API key, kills the old one instantly, and audits the rotation; `RELAY_MASTER_KEY_PREVIOUS` gives master-key rotation a verify-only window so unsubscribe links already sitting in delivered mail keep working — a dead unsubscribe link is a compliance failure | `api/routes.py`, `ingest/unsubscribe.py` |

What code cannot close — the production-posture, legal, and review
items, plus the three deliberately parked decisions — is recorded in
[docs/phase3-readiness.md](docs/phase3-readiness.md).

---

## Phase 4 — productization & scale (in progress)

| Capability | Where |
| --- | --- |
| **Self-serve onboarding**: `POST /internal/tenants/onboard` provisions the full working chain — tenant, API key, registered lead source, campaign, quotas — in one atomic admin call; a new client starts without anyone hand-editing config | `api/routes.py` |
| **Per-tenant quotas & spend controls**: `tenants.daily_send_cap` overrides the global real-send cap when set; `tenants.monthly_spend_cap_units` is a rolling-30-day cost ceiling — at/over it, NEW pipeline runs refuse to start as a recorded, audited kill (`killed_tenant_spend_cap`) while in-flight runs finish under their own budget. Alerts warn at 80% and go critical at 100%, before and as the wall hits | `db/models.py`, `guardrails/harness.py`, `domain/eligibility.py`, `observability/alerts.py` |
| **Cost attribution**: `GET /economics` — the client-profitability view: cross-campaign funnel, total and rolling-30d spend, cost per booked meeting (USD when calibrated), and headroom under the monthly cap | `economics.py` |
| **Multi-tenant concurrency, proven**: two tenants walk full cohorts through racing pipelines and workers simultaneously; each tenant's RLS view afterwards contains exactly its own rows | `tests/test_phase4_scale.py` |
| **Schema evolution seam**: `db/sql/001_schema_evolution.sql` carries idempotent ALTERs for existing databases (`metadata.create_all` only creates missing tables) | `db/sql/` |

---

## Exit gate — every item is a passing test

Run them: `just test-exit-gate` (or `just test` for the full suite).

| Roadmap exit-gate item | Proven by |
| --- | --- |
| An empty pipeline moves a fake lead through every state; the log traces its full journey | `tests/test_exit_gate_journey.py::test_fake_lead_walks_every_state` |
| A forced infinite loop is stopped by the iteration cap | `tests/test_guardrails.py::test_forced_infinite_loop_is_killed_by_iteration_cap` |
| An over-budget run is stopped by the budget ceiling | `tests/test_guardrails.py::test_over_budget_run_is_killed_by_budget_ceiling` |
| Reprocessing the same lead is a no-op | `tests/test_exit_gate_journey.py::test_reprocessing_closed_lead_is_noop` |
| The idempotency DB constraint rejects a duplicate | `tests/test_idempotency.py::test_duplicate_send_job_rejected_by_db_constraint` |
| No code path can send while `dry_run` is set | `tests/test_dry_run.py` (seven angles, incl. raw-SQL attacks) |
| A cross-tenant read/transition is rejected | `tests/test_tenant_isolation.py` |
| Suppressed recipients can never become send-eligible (§10 hard invariant) | `tests/test_suppression.py` |
| PII never reaches logs or audit payloads | `tests/test_logging_and_audit.py` |
| No prospect enters the datastore without lawful provenance (§7) | `tests/test_source_register.py` |

The suite runs against **real PostgreSQL** — RLS, triggers, and unique
constraints are the subjects under test, and they do not exist in mocks.

Phase 0 also went through an adversarial multi-agent review; the confirmed
findings are fixed and pinned by `tests/test_review_fixes.py` — approved
drafts are content-frozen (tamper-evident), a send job's recipient must be
its own lead's address, real-intent leads are blocked rather than silently
"sent" in simulation, retry-cap columns are immutable to the code they
police, `error_retryable` may only resume to its recorded state, the
cross-tenant suppression probe is closed, and the `SECURITY DEFINER`
functions carry owner-scoped policies so they keep working under `FORCE`
row-level security on managed Postgres (not only on a superuser owner).

---

## Quickstart

Prerequisites: [uv](https://docs.astral.sh/uv/),
[just](https://just.systems/), and either Docker or a local
PostgreSQL 16.

```bash
just sync                 # install dependencies
cp .env.example .env      # then edit values

# Database — pick one:
just infra-up             # Docker: Postgres + Redis + Mailpit
just db-local-start       # no Docker: throwaway local cluster on :5433

just db-migrate           # schema + triggers + RLS + rule seeding
just demo                 # walk a synthetic lead through every state
just seed                 # Phase 1A: seed + run a 20-prospect cohort
just test                 # the whole suite incl. exit gates
just api                  # FastAPI on :8000 (docs at /docs, review UI at /review)
just worker               # one internal send-worker pass
just stack-up             # optional: adds the n8n spine on :5678
```

`just demo` prints the full journey — 20 transitions from `created` to
`closed`, with the human gate and the simulated send made explicit.

To wire the spine: open n8n (http://localhost:5678), import
`infra/n8n/relay-spine.json`, and set `RELAY_API_BASE_URL` +
`RELAY_ADMIN_TOKEN` in the n8n environment.

---

## Architecture in one paragraph

A deterministic **spine** (n8n) sequences pipeline steps by calling the
**backend API** (FastAPI); all state lives in the **canonical
datastore** (Postgres), which is also the enforcement layer: the state
machine, tenant isolation, suppression, idempotency, and the dry-run
guarantee are triggers, RLS policies, and unique constraints — they hold
even against raw SQL. The **reasoning layer** is invoked at decision
points through the **task-routing seam** (local tier for cheap bounded
work, hosted tier where being wrong cascades — stubs in Phase 0), always
inside the **guardrail harness** (iteration cap, budget ceiling: dumb
limits that work when the intelligent component is the thing that
broke). Anything that would leave the machine passes two independent
gates: a **human approves the exact message version**, and the
**send-eligibility gate** re-checks lawfulness, suppression, and
idempotency at execution time. Simulated jobs always use the simulated
sender — that pairing is not configurable; the real executor (SES
sandbox, Phase 1C) exists only behind the §6 pilot gates.

Safety posture, layered (all must fail together for a bad send):
campaign/lead `dry_run` defaults (immutable) → DB trigger rejecting
real jobs for dry-run chains → eligibility gate failing real mode on
ten checks (incl. the pilot allowlist and daily cap) →
`RELAY_REAL_SEND_ENABLED=false` master switch → the sender's last-hop
recipient-hash and allowlist refusal → SES sandbox itself (AWS refuses
unverified recipients).

## Configuration

All configuration is environment-driven (pydantic-settings, `RELAY_`
prefix) — see `.env.example`. No secrets in code; API keys are stored
as SHA-256 hashes; per-tenant encryption keys derive from a master key
(KMS-managed in production, Phase 3).

The two compute tiers are provider-agnostic. A typical dev pairing —
Gemini Flash orchestrating over a Gemma workhorse, both on one Google
API key:

```bash
RELAY_COMPUTE_HOSTED_BACKEND=google   RELAY_HOSTED_MODEL=gemini-3.5-flash
RELAY_COMPUTE_LOCAL_BACKEND=google    RELAY_LOCAL_MODEL=gemma-4-31b-it
RELAY_GOOGLE_API_KEY=...
```

Swapping the orchestrator to a Claude model later is three lines:
`RELAY_COMPUTE_HOSTED_BACKEND=anthropic`, `RELAY_HOSTED_MODEL=<model>`,
`RELAY_ANTHROPIC_API_KEY=<key>`. The test suite never touches real
providers regardless of your `.env` — conftest pins both tiers offline.

Two database roles: the schema-owning admin role runs migrations
(`just db-migrate`); the API and worker run as `relay_app`, a
non-superuser subject to forced row-level security. The worker never
operates outside a tenant scope — it discovers tenants with queued work
via a SECURITY DEFINER function and processes each under that tenant's
RLS context.

## Repository layout

```
src/relay/
  api/            FastAPI boundary (schemas, tenant auth, routes, review/ops UIs)
  compute/        provider-agnostic LLM backends behind the routing seam
  crm/            one-way CRM mirror seam (never on the send path)
  db/             engines/sessions, ORM models, migrations, SQL (triggers, RLS)
  domain/         states, state machine, suppression, eligibility, approval, DSR
  evals/          golden-set invariants for the configured backends
  guardrails/     the harness: iteration cap, budget ceiling
  ingest/         SES/SNS event ingestion (signature-verified)
  observability/  metrics + alert rules, derived on read
  pipeline/       the runner: real data flow, resumability, crash recovery
  routing/        task→tier routing seam
  senders/        provider seam: simulated + SES (real senders behind §6 gates)
  synthetic/      seeded Faker prospects incl. adversarial edge cases
  workers/        internal-only workers: send, SES event poller, retention
  config.py, logs.py, hashing.py, tenancy.py, audit.py, economics.py, ...
tests/            exit-gate suite (runs against real Postgres)
infra/n8n/        the spine workflow (import into n8n)
scripts/          demo_journey.py, seed_synthetic.py, run_evals.py, dev_pg.sh
```

## Development

CI (GitHub Actions) runs ruff + the full test suite against a Postgres
16 service on every push. Locally: `just lint`, `just fix`,
`just test-cov`.

## What comes next (roadmap)

- **Real-prospect sending** — gated behind the §6 revisit criteria in
  [the sending-provider decision record](docs/decisions/sending-provider.md);
  the Smartlead enrollment adapter is deliberately deferred until then.
- **Multi-step sequences** — the send path currently pins
  `sequence_step = 1`; the duplicate/idempotency check must be
  generalized before step 2 exists.
- **Phase 4 remainder** — per-tenant mailbox/domain isolation model for
  real sending at volume, concurrency scaling past the single-process
  worker, self-serve configuration UI.
- **Production posture** — KMS-managed master key + email-hash pepper,
  per-mailbox capacity model, the §6 provider revisit.
