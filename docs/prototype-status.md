# PROTOTYPE STATUS — read this first if you are picking RELAY back up

(New to the codebase entirely? [onboarding.md](onboarding.md) is the
guided ramp; this file is the status ledger.)

**As of 2026-07-05, RELAY is a prototype.** Every roadmap phase (0, 1A,
1B, 1C, 2, 3, 4) is code-complete with 331 passing tests, and a live
SES-sandbox smoke was executed and recorded
([phase1c-live-smoke.md](phase1c-live-smoke.md)). What is NOT done was
skipped by an explicit, dated operator decision — not forgotten:

> Operator decision (2026-07-05): "since this is a prototype, anything
> that can be developed without progress being stopped should just be
> done. The legal stuff is not technically my responsibility yet …
> avoid that stuff and finish all the other coding stuff."

## Not done, deliberately (the go-to-production checklist)

None of these block each other; items 1–2 block real-prospect email.

1. **Legal / Data Preflight artifact** — jurisdiction matrix
   (region → lawful bases), DPA/client contract, subprocessor list,
   incident-response process, abuse policy. Lawyer + operator work.
   *Where it plugs in:* the preflight admin endpoints
   (`/internal/preflight/*`, artifact pinned by SHA-256) and
   `RELAY_REGION_BASIS_RULES` (the jurisdiction matrix as config — the
   enforcement code already exists and fail-closes).
2. **§6 production sending posture** — SES production access (leave the
   sandbox; request runbook in [deploy/aws/README.md](../deploy/aws/README.md)),
   per-tenant domain/mailbox verification (now automated:
   `/internal/tenants/{id}/sender-identity/provision` + `/sync` create
   the SES identity, hand back the DKIM records, and flip the attest
   when AWS confirms — publishing DNS stays with the tenant), warmup
   plan, DMARC review cadence, and the direct-SES-vs-Smartlead provider
   decision
   ([decisions/sending-provider.md](decisions/sending-provider.md); the
   Smartlead adapter is interface-only, deliberately).
3. **KMS secrets** — move `RELAY_MASTER_KEY` and
   `RELAY_EMAIL_HASH_PEPPER` out of `.env` into a secrets manager; then
   set `RELAY_EMAIL_HASH_LEGACY_LOOKUP=false`
   ([decisions/email-hash-pepper.md](decisions/email-hash-pepper.md)).
4. **Human security + compliance review** — required by the Phase 3
   exit gate; the automated audits in this repo are input, not a
   substitute. Step-by-step:
   [security-review-checklist.md](security-review-checklist.md).
5. **Throughput target** — pick a number, then run
   `just bench <tenants> <leads> <concurrency>` on production-like
   hardware. (Reference point: this dev container sustained ~11
   leads/sec full-funnel, offline compute.)
6. **Calibrate `RELAY_COST_UNIT_USD`** — until then economics endpoints
   report abstract units, not dollars.
7. **SQS long polling in the event worker** — deferred Phase 3
   src-level change. `event_worker.py` calls SQS `receive_message` with
   `WaitTimeSeconds=0` (short polling). Low-stakes today: at the
   current events tick interval (~300s) that's ~12 receives/hour, deep
   inside the SQS free tier, so the tick interval — not the poll type —
   bounds both cost and bounce-to-suppression latency. The change, when
   done: set `WaitTimeSeconds=20` (long polling) **and** decide the
   events tick interval under real load — they work together;
   long-polling alone doesn't reduce suppression latency while the
   sleep dominates. *Trigger to do it:* real send volume where fast
   suppression of a bad domain matters.
8. **Prospecting integration (finding clients, not just working them)**
   — RELAY starts at "you already have a list"; criteria-based
   discovery via a licensed data provider (Apollo-class) is not built.
   The receiving side already exists end to end: `POST /leads/batch` →
   source-register provenance → scoring → the `/prospects` shortlist,
   so the build is an adapter seam in `ingest/` (mirror `senders/`:
   interface + one provider + offline stub) feeding batch intake, plus
   per-campaign ICP criteria. Provider evaluation is the slow part —
   open decision record:
   [decisions/prospecting-provider.md](decisions/prospecting-provider.md).
   *Trigger to do it:* sustained volume beyond hand-research (~hundreds
   of prospects/month) or the first external tenant needing self-serve
   discovery. Until then, manual research + `just import` is
   competitive and produces better notes.
9. **Reply-side scheduling** — `interested → booking_pending → booked`
   currently assumes a human books the meeting out-of-band. A calendar
   integration (Cal.com / Google Calendar API) proposing times in the
   reply thread and confirming the booking would close the last manual
   step of the funnel. Fits the machine as-is: `booking_pending` is the
   wait state; the integration is a new boundary adapter, not new
   states. *Trigger to do it:* enough interested replies that manual
   scheduling becomes the bottleneck — a good problem, not yet real.

## Why you can trust the "not done" list is complete

The system fail-closes on every one of these: real-person data is
rejected at INSERT without an approved preflight record; real sends
fail named eligibility checks without the §6 attests; unlisted regions
are blocked the moment region rules exist. If something were missing
from this list, the gates would say so at runtime — by refusing.

Full ledgers: [phase3-readiness.md](phase3-readiness.md) ·
[phase4-readiness.md](phase4-readiness.md) · §17 of the project
documentation (Plan branch).

## Operational notes for a fresh environment

- `.env` is never committed. A fresh container needs: the pilot AWS
  credentials (IAM user `relay-ses-pilot`), `RELAY_PILOT_RECIPIENTS`,
  and a local Postgres (`just db-local-start` + `just db-migrate`).
- Real sends: `RELAY_REAL_SEND_ENABLED=false` at rest, enabled only in
  the sending process's environment; recipients only ever from the
  allowlist; every send human-approved.
