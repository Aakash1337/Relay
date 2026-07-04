# Decision record: local-tier tool calling (§8 open item)

**Status:** RESOLVED for Phase 2 — the local tier remains tool-free.
**Date:** Phase 2. **Revisit:** Phase 3, against the criteria below.

## The question

§8 left one routing rule provisional: may the local (cheap, bounded)
tier be trusted to call tools, or must every tool-calling task route to
the hosted tier?

## Decision

The local tier stays **structurally tool-free**:

1. `route(requires_tools=True)` forces the hosted tier regardless of the
   task's default route (`src/relay/routing/router.py`) — unchanged
   since Phase 0 and now pinned by test.
2. The backends that can serve the local tier have **no tool support by
   construction** — `OpenAICompatBackend` and `GoogleGeminiBackend`
   never send tool schemas; a confused or compromised local model can
   produce at most one JSON blob that downstream gates re-validate.

So this is not a policy a prompt can argue with; there is no code path
in which a local model receives or invokes a tool.

## Rationale

- The blast radius of a wrong tool call (writes, sends, external calls)
  is categorically larger than a wrong JSON answer, and the local tier
  exists precisely for tasks where being wrong is cheap.
- Small-model tool-call reliability varies wildly by model and schema;
  we have no validation evidence, and "seems fine" is not evidence.
- Nothing in Phases 0–2 needs local tools: every local task is
  text-in/JSON-out by design.

## Revisit criteria (Phase 3)

Open this only when all three hold:

1. A concrete task exists that needs tools AND is too high-volume for
   the hosted tier's economics.
2. A validation harness (extending `relay.evals`) measures tool-call
   schema compliance and argument correctness for the candidate local
   model over ≥200 golden cases, with a pass bar ≥99% for schema
   compliance and an explicit review of failure modes.
3. The tools exposed are read-only or idempotent, and every effect they
   could cause remains behind the existing gates (suppression,
   eligibility, human approval).

Until then, `requires_tools=True` → hosted, everywhere, structurally.
