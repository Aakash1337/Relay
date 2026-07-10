# Learn RELAY like you own it

This guide is for someone who does not usually read Python projects and wants a
practical route from “I do not know where to start” to “I can explain, debug,
and change this system without guessing.”

Do not begin by reading every file. RELAY has a small number of load-bearing
ideas. Learn those ideas, follow one lead through the system, and only then fill
in the surrounding modules.

## The whole project in five sentences

1. A lead is a database row whose `state` acts like the program counter.
2. The pipeline runner reads that state, performs one legal step in one
   transaction, advances the state, and repeats until it reaches a stop.
3. Human approval and sending are separate: approval freezes exact content;
   a worker later re-checks every safety rule before sending.
4. PostgreSQL enforces the irreversible rules—tenant isolation, legal state
   transitions, suppression, dry-run safety, append-only audit, and duplicate
   prevention—even if Python is wrong.
5. Language models propose data; they cannot approve, send, change permissions,
   or bypass a database rule.

If you can keep those five sentences in your head, the repository stops looking
like a pile of unrelated folders.

## First: translate the Python project into familiar concepts

You do not need to become a Python expert before reading RELAY. These are the
pieces of Python-project vocabulary that matter here.

| You see | Read it as |
| --- | --- |
| `pyproject.toml` | The project manifest: runtime packages, developer tools, test and lint configuration. |
| `uv.lock` | Exact dependency versions. Similar to a lockfile in JavaScript or another package manager. |
| `src/relay/` | The application package. `relay.foo` imports come from here. |
| `__init__.py` | Marks a directory as a Python package; often it only re-exports names. Usually skim it. |
| `def name(...):` | A function. Type hints after arguments and `->` describe expected types. |
| `class Name:` | A class. In this project, classes often represent settings, database rows, backends, or a runner with state. |
| `@router.post(...)` | A decorator: register the following function as an HTTP endpoint. |
| `@dataclass` | A small data container whose constructor and comparisons are generated. |
| `with tenant_session(...) as session:` | Open a database transaction and guarantee cleanup/commit/rollback at the block boundary. |
| `raise SomeError(...)` | Stop this path with a named failure. Follow where that exception is caught to learn recovery behavior. |
| `pytest` fixtures | Reusable test setup. `tests/conftest.py` creates the real PostgreSQL environment used by tests. |
| Pydantic `BaseModel` | Typed validation at an input/output boundary, especially API payloads and settings. |
| SQLAlchemy `select(...)` | A Python-built SQL query. Models in `db/models.py` describe rows and relationships. |

Three reading rules save time:

- Read function names, inputs, return types, and exceptions before reading the
  body.
- When a function writes state, find its test immediately; the test usually
  explains the contract more clearly than comments do.
- When Python and SQL both enforce something, treat the SQL as the final safety
  boundary and the Python as the readable orchestration layer.

## Your first 90 minutes

### 1. Look at the pictures, not the code (15 minutes)

Read [the README](../README.md) through “Architecture.” Study these diagrams:

- `img/lead-flow.svg`: what happens to one lead.
- `img/architecture.svg`: which processes exist.
- `img/change-map.svg`: where a change belongs.

Say this aloud afterward: “The API and pipeline make decisions; the database
holds state and enforces invariants; workers perform delayed work; n8n is only a
timer; external providers sit at the edges.”

### 2. Run the deterministic version (20 minutes)

Use the offline backends while learning. They are fast, deterministic, spend no
quota, and cannot make a real send.

In PowerShell, set these for the terminal you are using:

```powershell
$env:RELAY_COMPUTE_LOCAL_BACKEND = "offline"
$env:RELAY_COMPUTE_HOSTED_BACKEND = "offline"
$env:RELAY_SENDER_PROVIDER = "none"
$env:RELAY_REAL_SEND_ENABLED = "false"
```

Start the PostgreSQL instance described in [Getting started](../README.md#getting-started),
run `just db-migrate`, then:

```powershell
just demo
just seed 20
```

For `just demo`, copy the printed transition list onto paper. Mark the three
actors:

- `system:pipeline`
- `human:...`
- `worker:send`

The actor changes are the architecture. The pipeline deliberately stops before
the human and before the worker.

### 3. Connect output to four files (25 minutes)

Open these in order:

1. `src/relay/domain/states.py` — legal states and edges.
2. `src/relay/domain/state_machine.py` — the normal Python transition path.
3. `src/relay/pipeline/runner.py` — advance until waiting or terminal.
4. `scripts/demo_journey.py` — a small operator script that connects the pieces.

Do not read every line. For each transition printed by the demo, answer:

- Which state was read?
- Which handler ran?
- What state was written?
- What makes that write legal?
- What would happen if the process died before commit?

Then read [How control actually flows](control-flow.md). It will make much more
sense after you have seen a real trace.

### 4. Read the tests as executable documentation (30 minutes)

Start with one test file per guarantee:

| Question | Test file |
| --- | --- |
| Can a lead complete the funnel? | `tests/test_exit_gate_journey.py` |
| Can two workers send twice? | `tests/test_idempotency.py` and `tests/test_adversarial.py` |
| Can one tenant see another? | `tests/test_tenant_isolation.py` |
| Can dry-run data send? | `tests/test_dry_run.py` |
| Can an unsubscribed address return? | `tests/test_suppression.py` and `tests/test_unsubscribe.py` |
| Can prompt injection change authority? | `tests/test_compute.py` and `tests/test_evals.py` |
| Does erasure survive backup/restore? | `tests/test_adversarial.py` |

Read the test name and assertions first. Only read fixture setup when you need to
know how the state was created.

At the end of 90 minutes, you should be able to draw one lead’s journey and
explain why approval does not send.

## The seven-session ownership path

Work in sessions of 45–60 minutes. Stop when you can answer the checkpoint;
more reading after your attention is gone is not useful.

### Session 1 — State machine and transactions

Read:

1. `src/relay/domain/states.py`
2. `src/relay/domain/state_machine.py`
3. `src/relay/pipeline/runner.py`
4. `src/relay/db/sql/002_functions.sql`
5. `src/relay/db/sql/003_triggers.sql`

Checkpoint: explain how an illegal transition is rejected by Python and again
by PostgreSQL. Explain why one step per transaction makes a crash restartable.

### Session 2 — Database and tenant isolation

Read:

1. `src/relay/db/models.py` (skim table names and constraints)
2. `src/relay/db/engine.py`
3. `src/relay/db/migrate.py`
4. `src/relay/db/sql/004_rls.sql`
5. `tests/test_tenant_isolation.py`

Learn the two roles:

- `relay`: schema owner and administrative migration role.
- `relay_app`: runtime role forced through row-level security.

Checkpoint: explain why forgetting `WHERE tenant_id = ...` in Python still
cannot reveal another tenant’s rows through the runtime connection.

### Session 3 — Approval and the send boundary

Read:

1. `src/relay/domain/approval.py`
2. `src/relay/domain/eligibility.py`
3. `src/relay/workers/send_worker.py`
4. `src/relay/senders/registry.py`
5. `src/relay/senders/simulated.py`
6. `src/relay/senders/ses.py`

Write down each layer that must agree before a real send. Notice that the worker
re-runs eligibility immediately before the provider call.

Checkpoint: explain what uniquely prevents each of these: unapproved content,
suppressed recipient, duplicate job, wrong tenant, excessive volume, and an
accidental real send during development.

### Session 4 — Feedback, suppression, and erasure

Read:

1. `src/relay/ingest/ses_events.py`
2. `src/relay/workers/event_worker.py`
3. `src/relay/ingest/unsubscribe.py`
4. `src/relay/domain/suppression.py`
5. `src/relay/domain/dsr.py`
6. `src/relay/hashing.py`

Checkpoint: tell the complete story of a hard bounce and a one-click
unsubscribe. Both stories must end with every future send being blocked.

### Session 5 — Models, prompts, and guardrails

Read:

1. `src/relay/compute/base.py`
2. `src/relay/compute/prompting.py`
3. `src/relay/compute/registry.py`
4. `src/relay/compute/google_api.py`
5. `src/relay/routing/router.py`
6. `src/relay/routing/executors.py`
7. `src/relay/guardrails/harness.py`
8. `src/relay/evals/harness.py`

Run offline evals first, then live evals only when intentionally spending quota:

```powershell
uv run python scripts/run_evals.py both
```

Checkpoint: explain why a model can produce bad JSON or a bad score but still
cannot send an email. Also explain the current live Gemini issue documented as
A13 in [the audit](project-audit-2026-07-05.md#a13--hosted-model-json-output-is-nondeterministic-in-live-use).

### Session 6 — HTTP product surface and operator UI

Read:

1. `src/relay/api/app.py`
2. `src/relay/api/deps.py`
3. `src/relay/api/schemas.py` by feature, not top to bottom
4. `src/relay/api/routes.py` one endpoint group at a time
5. `src/relay/api/review_ui.py`, `ops_ui.py`, and `admin_ui.py`

Run the API and open `/docs`, `/review`, `/ops`, and `/admin`. Pick one endpoint
and trace it in both directions:

```text
HTTP request
  → Pydantic request model
  → auth dependency
  → tenant/admin session
  → domain function
  → database rule
  → response model
```

Checkpoint: given any route, identify its authentication boundary, database
role, business-rule function, and test.

### Session 7 — Operations and deployment

Read this last:

1. `src/relay/observability/metrics.py`
2. `src/relay/observability/alerts.py`
3. `infra/n8n/relay-spine.json`
4. [Manual deployment](deployment.md)
5. [Container and GCP deployment](../deploy/README.md)
6. [Prototype status](prototype-status.md)
7. [Security review checklist](security-review-checklist.md)
8. [Executed audit](project-audit-2026-07-05.md)

Checkpoint: separate “works as a prototype” from “ready for production.” Name
the external prerequisites and the highest-priority open audit findings.

## What to ignore until the core clicks

These are real parts of the project, but they are poor starting points:

- Terraform and Cloudflare setup
- n8n JSON details
- EspoCRM adapter details
- HTML/CSS in the three operator pages
- provider-specific SDK details
- every field in every API schema
- every historical phase document

Return to them after you can explain the state machine, database enforcement,
human gate, worker handoff, and feedback loop.

## How to investigate any unfamiliar behavior

Use the same six-step method every time:

1. Name the observable behavior in one sentence.
2. Find the closest test with `rg "keyword" tests src`.
3. Identify the state before and after the behavior.
4. Find the domain function or worker that performs it.
5. Check the matching SQL trigger, constraint, or RLS policy.
6. Run the smallest relevant test, then the full suite before changing code.

Example: “Why did this lead not send?”

1. Read the lead state and trace endpoint.
2. If `approval_pending`, no human approved it.
3. If `send_queued`, inspect the job and run the worker.
4. If `send_blocked`, read the recorded eligibility failures.
5. If `error_retryable`, inspect the provider/compute failure and retry count.
6. Check suppression, caps, identity attestations, and the provider registry.

This is much faster than reading `routes.py` from line 1 and hoping the answer
appears.

## Safe exercises that create real understanding

Do these on synthetic/test data with real sending disabled.

1. Change the fit threshold and observe qualified versus rejected branches.
2. Try an illegal state update through raw SQL and read the trigger error.
3. Add a suppression entry after a job is queued but before the worker runs;
   watch execution-time eligibility block it.
4. Race two worker calls and verify only one send-job execution succeeds.
5. Create two tenants and attempt a cross-tenant read using the wrong key.
6. Add hostile instructions to a synthetic bio and inspect the wrapped prompt.
7. Kill a pipeline run mid-step, run recovery, and inspect the audit trail.
8. Follow one unsubscribe token from creation to permanent suppression.

For each exercise, predict the result before running it. If your prediction is
wrong, that gap is exactly what you should study next.

## The “back of my hand” checkpoint

You understand RELAY when you can answer these without opening the repository:

1. What are the three actors that move a lead?
2. Where are legal state transitions defined and enforced?
3. Why can approval not send?
4. Why can two workers not double-send?
5. Why can one tenant not read another tenant’s rows?
6. What happens when a model times out, refuses, or returns invalid JSON?
7. What happens after a hard bounce, complaint, or unsubscribe?
8. What remains after DSR erasure, and why?
9. Which settings must agree before a real send?
10. Which parts are proven locally and which still require real AWS/GCP/CRM
    environments?
11. What are the top five open risks in the current audit?
12. Where would you add a state, eligibility rule, endpoint, database column,
    provider, or metric?

The final test is teaching it. Give a ten-minute walkthrough to an imaginary new
engineer using only the lead-flow diagram. Wherever you get vague, return to the
corresponding session above.

## Your daily command card

```powershell
just sync             # synchronize dependencies
just db-migrate       # apply idempotent schema, triggers, RLS, and state rules
just demo             # one lead through the funnel
just seed 20          # a synthetic cohort
just test-exit-gate   # core product guarantees
just test             # full PostgreSQL suite
just test-cov         # coverage and missed lines
just lint-ruff        # fast lint gate
just fmt-check        # formatting check
just api              # local API and operator pages
just worker           # one send-worker pass
just retention        # retention/recovery pass
just events           # one SQS event-worker pass when configured
just evals both       # configured model evals; may spend provider quota
just bench 2 10 4     # measure full-funnel throughput
```

Keep `RELAY_REAL_SEND_ENABLED=false` while learning. When you intentionally test
real sending, enable it only in the worker process after the allowlist,
attestations, preflight, provider credentials, and exact approved recipient have
all been verified.
