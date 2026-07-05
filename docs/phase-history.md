# RELAY — build history by phase

How the system was built, phase by phase, with each phase's capability
table preserved as it stood at completion. The README describes the
finished system; this file records the order and reasoning of its
construction. The full roadmap and project documentation live on the
`Plan` branch.

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

## Phase 4 — productization & scale

| Capability | Where |
| --- | --- |
| **Self-serve onboarding**: `POST /internal/tenants/onboard` provisions the full working chain — tenant, API key, registered lead source, campaign, quotas — in one atomic admin call; a new client starts without anyone hand-editing config | `api/routes.py` |
| **Per-tenant quotas & spend controls**: `tenants.daily_send_cap` overrides the global real-send cap when set; `tenants.monthly_spend_cap_units` is a rolling-30-day cost ceiling — at/over it, NEW pipeline runs refuse to start as a recorded, audited kill (`killed_tenant_spend_cap`) while in-flight runs finish under their own budget. Alerts warn at 80% and go critical at 100%, before and as the wall hits | `db/models.py`, `guardrails/harness.py`, `domain/eligibility.py`, `observability/alerts.py` |
| **Cost attribution**: `GET /economics` — the client-profitability view: cross-campaign funnel, total and rolling-30d spend, cost per booked meeting (USD when calibrated), and headroom under the monthly cap | `economics.py` |
| **Multi-tenant concurrency, proven**: two tenants walk full cohorts through racing pipelines and workers simultaneously; each tenant's RLS view afterwards contains exactly its own rows | `tests/test_phase4_scale.py` |
| **Per-tenant sending identity**: `tenants.sender_from_address` — each client sends as its own (provider-verified) address; NULL falls back to the global `RELAY_SES_FROM`. The worker resolves it and hands it to the sender; identity *verification* remains a §6 operator attest | `db/models.py`, `workers/send_worker.py`, `senders/ses.py` |
| **Worker concurrency**: `relay-worker --concurrency N` drains tenants in parallel threads. Tenants are independent streams — per-job transactions, SKIP LOCKED claims, per-tenant advisory-lock cap serialization — so parallelism changes throughput, not semantics | `workers/send_worker.py` |
| **Schema evolution seam**: `db/sql/001_schema_evolution.sql` carries idempotent ALTERs for existing databases (`metadata.create_all` only creates missing tables) | `db/sql/` |

The exit-gate ledger — what is pinned by tests versus what needs an
operator decision (throughput target, per-tenant §6 posture) — is
[docs/phase4-readiness.md](docs/phase4-readiness.md).

Prototype utilities on top of the phases:

| Capability | Where |
| --- | --- |
| **Multi-step sequences**: `sequence_length`/`sequence_delay_hours` per campaign; step N+1 re-enters the pipeline loop after the no-reply delay, drafts its own version, and needs its own §10 approval; reply/bounce/unsubscribe/suppression cancel structurally | `pipeline/runner.py`, `domain/states.py` |
| **Region-rules seam**: `RELAY_REGION_BASIS_RULES` (JSON region → allowed lawful bases) enforced at the eligibility gate; empty = permissive placeholder, and once rules exist an unlisted region is blocked. The Legal/Data Preflight's jurisdiction matrix becomes a config edit, not code | `config.py`, `domain/eligibility.py` |
| **Throughput benchmark**: `uv run python scripts/benchmark_throughput.py --tenants N --leads M --concurrency C` — per-phase wall-clock and leads/sec against real Postgres, the instrument for the Phase 4 throughput gate | `scripts/benchmark_throughput.py` |
| **Admin console**: `/admin` — self-contained page over the admin API (onboard, rotate key, attest sender identity, global suppression); adds convenience, no capability — every action is token-gated server-side | `api/admin_ui.py` |

---
