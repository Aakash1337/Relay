# Phase 4 — Productization & Scale: status against the exit gate

Phase 4's exit gate is partly operational ("target throughput sustained"
needs a real target; real multi-tenant sending needs the §6 production
posture). This records what the codebase now structurally provides,
each item pinned by tests, and what remains an operator decision.

## Exit gate, item by item

| Exit-gate item | Status | Mechanism |
| --- | --- | --- |
| A new client is onboarded without hand-editing config | **Done (code)** | `POST /internal/tenants/onboard`: tenant + API key + source + campaign + quotas + sender identity, one atomic admin call. Pinned by `test_onboarding_provisions_a_working_chain`. |
| Two tenants run simultaneously with verified data and sending isolation | **Done (code)** | Racing pipelines + workers across two tenants; RLS row sets verified exact afterwards; per-tenant `sender_from_address` proves *sending* isolation at the provider boundary. Pinned by `test_two_tenants_run_concurrently_with_isolation` and `test_tenant_sender_identity_overrides_global_from`. |
| Per-client cost and profitability are visible | **Done (code)** | `GET /economics`: cross-campaign funnel, total + rolling-30d spend, cost per booked meeting, headroom under the monthly cap. USD appears once `RELAY_COST_UNIT_USD` is calibrated. |
| Target throughput sustained under concurrent multi-tenant load | **Mechanism done; target is the operator's** | The worker scales across tenants (`relay-worker --concurrency N`): tenants are independent streams (per-job transactions, SKIP LOCKED claims, per-tenant advisory-lock cap serialization), so parallelism changes throughput, not semantics. Pinned by `test_concurrent_worker_drains_multiple_tenants_in_one_pass`. A *sustained* throughput claim needs a real load target and environment — set one, then benchmark. |

## Quotas & spend controls (supporting the gate)

- `tenants.daily_send_cap` — per-tenant real-send cap override.
- `tenants.monthly_spend_cap_units` — rolling-30d cost ceiling; new
  runs refuse to start at the cap (recorded `killed_tenant_spend_cap`),
  with warning/critical alerts at 80%/100%.
- `tenants.sender_from_address` — per-tenant sending identity (NULL =
  global). Identity *verification* remains a §6 operator attest.

## Remaining operator items

- **Throughput target** — pick the number (leads/day per tenant, tenant
  count), then benchmark `--concurrency` against it in a
  production-like environment. The compute tier (single-GPU ceiling,
  §17) is the expected bottleneck, not the datastore.
- **Real multi-tenant sending posture** — per-tenant domain/mailbox
  verification, warmup per identity, and the §6 production-provider
  revisit. The code seam (`sender_from_address`, per-mailbox pacing) is
  ready; the attestation model today is still global, which is correct
  for the sandbox pilot and must become per-tenant with the §6 work.
- **Self-serve configuration UI** — the API is the self-serve surface
  today (`/internal/tenants/onboard`); a browser UI over it is a
  product decision, not a safety item.
- **KMS master key + pepper management** — unchanged from Phase 3
  readiness; the seams are ready.
