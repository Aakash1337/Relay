# RELAY

**Autonomous B2B sales prospecting and outreach. Outbound that compounds.**

RELAY automates the prospecting-to-booking funnel as an orchestrated,
observable pipeline: source → verify → score → personalize → **human
approval gate** → **send-eligibility gate** → send → reply triage →
booking. It is designed so that an unlawful, suppressed, or duplicate
send is *structurally impossible* — enforced by the database, not by
good intentions.

This repository currently implements **Phase 0 — Foundations &
Scaffolding** of the [development roadmap](RELAY-development-roadmap.md)
(on the `Plan` branch, together with the full
[project documentation](RELAY-project-documentation.md)): a skeleton
that runs end to end with **no real work in it** — synthetic data only,
no real PII, and no real send path at all.

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
| Hard `dry_run` flag on leads **and** campaigns — immutable, DB-enforced, and `RealSender` cannot even be constructed in Phase 0 | `src/relay/senders.py`, `fn_send_jobs_guard` |
| Backend API skeleton + health check; n8n spine workflow calling it | `src/relay/api/`, `infra/n8n/relay-spine.json` |
| Tenancy primitives: per-tenant key derivation, vector-store namespaces | `src/relay/tenancy.py`, `src/relay/hashing.py` |

**What Phase 0 deliberately does NOT contain:** real prospect data (gated
by the Legal/Data Preflight, Phase 1B), real sending (gated by
deliverability + provider approval, Phase 1C), any model calls (the
reasoning stubs land in Phase 1A), CRM sync, and the approval UI.

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
just test                 # the whole suite incl. exit gates
just api                  # FastAPI on :8000 (docs at /docs)
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
idempotency at execution time. In Phase 0 the executor is a simulated
sender; a real one does not exist yet, by design.

Safety posture, layered (all must fail together for a bad send):
campaign/lead `dry_run` defaults (immutable) → DB trigger rejecting
real jobs for dry-run chains → eligibility gate failing real mode on
seven checks → `RELAY_REAL_SEND_ENABLED=false` → `RealSender` raising on
construction.

## Configuration

All configuration is environment-driven (pydantic-settings, `RELAY_`
prefix) — see `.env.example`. No secrets in code; API keys are stored
as SHA-256 hashes; per-tenant encryption keys derive from a master key
(KMS-managed in production, Phase 3).

Two database roles: the schema-owning admin role runs migrations
(`just db-migrate`); the API and worker run as `relay_app`, a
non-superuser subject to forced row-level security. The worker never
operates outside a tenant scope — it discovers tenants with queued work
via a SECURITY DEFINER function and processes each under that tenant's
RLS context.

## Repository layout

```
src/relay/
  api/          FastAPI boundary (schemas, tenant auth, routes)
  db/           engines/sessions, ORM models, migrations, SQL (triggers, RLS)
  domain/       states, state machine, suppression, eligibility, approval
  guardrails/   the harness: iteration cap, budget ceiling
  pipeline/     the Phase 0 runner (stubbed steps, real control flow)
  routing/      task→tier routing seam + stub executors
  workers/      the internal-only send worker
  config.py, logs.py, hashing.py, tenancy.py, audit.py, senders.py
tests/          exit-gate suite (runs against real Postgres)
infra/n8n/      the spine workflow (import into n8n)
scripts/        demo_journey.py, dev_pg.sh
```

## Development

CI (GitHub Actions) runs ruff + the full test suite against a Postgres
16 service on every push. Locally: `just lint`, `just fix`,
`just test-cov`.

## What comes next (roadmap)

- **Phase 1A** — synthetic/seed dry-run MVP: real two-tier routing
  (local model + hosted API), Faker-generated prospects, simulated
  replies, one CRM sync target, minimal approval UI with the reviewer
  rubric, cost-per-qualified-meeting projection.
- **Phase 1B** — real data, no sending. Blocked by the Legal/Data
  Preflight artifact (jurisdiction matrix, controller/processor,
  retention, DSR/deletion workflow).
- **Phase 1C** — tiny real-send pilot. Blocked by deliverability
  basics, provider approval, audit trail.
- **Phase 2** — reliability: adversarial test suite, observability
  dashboards, evals, resumability, backpressure.
- **Phase 3–4** — production readiness and multi-tenant productization.
