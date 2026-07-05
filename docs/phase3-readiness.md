# Phase 3 — Production Readiness: status against the exit gate

Phase 3's exit gate cannot be closed by code alone: it requires real
outbound volume, monitored deliverability over time, and a human
security + compliance review. This document separates what the codebase
now **structurally provides** from what remains an **operator/legal
deliverable**, and records the deliberately parked decisions.

## What the code provides (each item pinned by tests)

| Exit-gate concern | Mechanism | Where |
| --- | --- | --- |
| Suppression before every send | eligibility gate + DB trigger on queue AND claim | `fn_is_suppressed`, `fn_send_jobs_guard` |
| Permanent unsubscribe, incl. one-click | RFC 8058 signed-token endpoint; suppression always lands, decoupled from lead state; tokens survive master-key rotation | `ingest/unsubscribe.py` |
| Bounce/complaint handling with automatic pausing | SNS-verified ingestion → auto-suppress → `campaign_below_thresholds` blocks further real sends | `ingest/ses_events.py`, `domain/eligibility.py` |
| Volume caps, warmup, pacing | daily cap (race-proof, advisory-lock serialized), hourly cap, min spacing, warmup ramp; pacing defers rather than blocks | `domain/eligibility.py`, `workers/send_worker.py` |
| Reputation monitoring | bounce/complaint rates in `/metrics` (+ Prometheus), `bounce_rate_high` critical alert with a min-sends floor | `observability/` |
| Human-in-the-loop at scale | confidence-ordered review queue, batch review endpoint (per-item transactions), edit-rate as a first-class metric | `api/routes.py`, `observability/metrics.py` |
| Retention / deletion / DSR | erasure leaves only the hashed do-not-contact entry; retention purge never fabricates an opt-out | `domain/dsr.py`, `workers/retention_worker.py` |
| DR: tested restore, in-flight durability | pg_dump→restore test proves erasure survives backups; crash recovery closes orphans on every tick | `tests/test_adversarial.py`, `pipeline/recovery.py` |
| Audit trail | append-only, redacted, every consequential action | `audit.py`, DB triggers |
| Secrets rotation | tenant API key rotation endpoint (old key dies instantly, audited); `RELAY_MASTER_KEY_PREVIOUS` verify-only rotation window | `api/routes.py`, `config.py` |
| Tenant isolation | FORCEd RLS on every tenant-bearing table, tested cross-tenant | `db/sql/004_rls.sql` |

## Operator / legal deliverables (code cannot close these)

- **Production sending posture** — leaving the SES sandbox, dedicated
  authenticated domains at volume, DMARC report review cadence,
  inbox-placement monitoring. Gated by the §6 revisit criteria in
  [the sending-provider decision record](decisions/sending-provider.md).
- **Region-specific suppression / lawful-basis rules** — the
  `lawful_send_basis` check is a named seam awaiting the Legal/Data
  Preflight's jurisdiction matrix (GDPR / CASL / CAN-SPAM, verified
  current at build time). Code must not invent this.
- **Client contract / DPA, subprocessor list, incident-response
  process, abuse-prevention policy** — human/legal documents.
- **KMS-managed master key** — the derivation seam is ready
  (`derive_tenant_key`); swapping the dev master key for KMS is a
  deployment change plus the parked pepper decision below.
- **Human security + compliance review** — the exit gate requires it
  explicitly; an automated audit is input to it, not a substitute.

## Parked decisions (deliberate, awaiting the operator)

1. **Email-hash HMAC pepper** — `hash_email` is unkeyed SHA-256; a
   DSR-erased suppression hash is theoretically reversible by guessing
   a known address. Peppering changes every stored digest, so it needs
   a migration plan; fold into the KMS/master-key work.
2. **Global-scope suppression cross-tenant asymmetry** — any tenant can
   insert a `scope='global'` row that silently blocks every other
   tenant's sends, which those tenants can neither see nor remove.
   Over-suppression is the safe direction, but the asymmetry needs a
   deliberate multi-tenant decision before Phase 4.
3. **`sequence_step == 1` hardcoded** in the idempotency/duplicate
   check — must be generalized before multi-step sequences ship.
