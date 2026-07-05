# Decision: keyed email digests (pepper) with a dual-lookup transition

**Date:** 2026-07-05 · **Status:** decided, implemented
**Decider:** operator · **Relates to:** §11 security, §17 (suppression scope), Phase 3 KMS/master-key work

## Problem

`hash_email` was unkeyed SHA-256. Email addresses are guessable, so an
unkeyed digest is reversible by anyone holding a candidate address —
which defeats the point of storing only a hash for DSR-erased
do-not-contact entries ("we deleted everything except a marker" must not
mean "a marker anyone can reverse").

## Decision

- `hash_email` is now **HMAC-SHA256 under `RELAY_EMAIL_HASH_PEPPER`** —
  a long-lived secret managed alongside the master key (KMS in
  production). Unlike the master key it must **not** rotate casually:
  every stored digest depends on it. There is deliberately no
  previous-pepper seam; treat the pepper as write-once until a
  re-digesting migration exists.
- **Dual-lookup transition**: reads that match long-lived hash columns
  (suppression checks, provider-event lead/tenant matching, DSR erasure,
  the sender's last-hop cross-checks, the pilot allowlist) test every
  candidate digest via `email_hash_candidates()` — peppered first, then
  the legacy unkeyed digest while `RELAY_EMAIL_HASH_LEGACY_LOOKUP=true`
  (the default). **Writes are always peppered.**
- **Cutover**: once no pre-pepper digests remain (a fresh
  `relay-migrate --reset`, or a data set created entirely after this
  change), set `RELAY_EMAIL_HASH_LEGACY_LOOKUP=false` and the candidate
  set collapses to the peppered digest alone.

## Transition caveat (accepted, time-boxed)

The DB triggers (`fn_send_jobs_guard`'s suppression re-check,
`fn_auto_suppress`) compare **stored** hashes and therefore only match
suppression rows of the same digest era as the lead/job row. During the
dual-lookup window the cross-era coverage comes from the code-level
gates (which check all candidates); the trigger backstop regains full
strength at cutover. The pilot database is effectively fresh, so the
window here is zero; deployments with historical data should plan the
cutover deliberately.

## What did NOT change

- `hash_api_key` stays unkeyed SHA-256: API keys are high-entropy random
  strings, not guessable identifiers — a pepper adds nothing.
- `derive_tenant_key` (master key) is a separate seam with its own
  rotation mechanism (`RELAY_MASTER_KEY_PREVIOUS`).
- Historical log lines keep their old digest prefixes; log correlation
  is per-scheme by nature.
