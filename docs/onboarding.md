# Onboarding — reading RELAY from zero

A path from "I just cloned this" to "I can work on any part of it and
explain why it's built this way." Written for someone who hasn't built
this category of product before. Work through it in order; each session
ends with what you should be able to explain afterward.

## Part 1 — what you're looking at

The one-paragraph version: RELAY is a multi-tenant B2B cold-outreach
pipeline. It takes a list of prospects and runs the whole funnel —
research, scoring, drafting a personalized email, human approval,
sending, and handling whatever comes back (replies, bounces,
unsubscribes, booked meetings). LLMs do the repetitive thinking, a
human approves every email before it can leave, and the compliance
rules are enforced *inside PostgreSQL* as triggers and constraints —
so a duplicate send or an email to someone who unsubscribed isn't a bug
you hope to catch; it's a transaction that can't commit.

Three ideas generate almost every design decision in this repo. Read
everything else through them:

1. **Postgres is the enforcement layer, not just storage.** Tenant
   isolation is row-level security, the lead lifecycle is a
   trigger-enforced state machine, duplicate-send protection is a
   UNIQUE constraint, the audit log is append-only by trigger.
   Application code can be buggy, an LLM can be confused, someone can
   run raw SQL — the database refuses anyway.
2. **The state machine IS the control flow.** A lead's `state` column
   is the program counter. There is no main loop; independent drivers
   (pipeline runner, send worker, event worker) each read the state, do
   the one legal next step, and write the new state back — one
   transaction per step. That's why a crash anywhere is harmless and
   why every process is restartable at any moment.
3. **The models have no authority.** LLMs are stateless functions
   behind a config seam. Their output is data that deterministic gates
   re-check; they cannot send, approve, or unsuppress anything.
   External text (bios, replies) is escaped and wrapped in labeled
   `<untrusted_data>` tags before reaching a prompt.

The shape of the system (matches `img/architecture.svg`): one FastAPI
service, one Postgres, internal workers sharing the same codebase, an
n8n workflow (or cron) as the timer, AWS SES/SNS/SQS only when real
mail moves, four plain-HTML pages (`/prospects`, `/review`, `/ops`,
`/admin`).

## Part 2 — the repo map

```
src/relay/
  api/            FastAPI: routes.py (every endpoint), auth, schemas,
                  and the four HTML pages (review, prospects, ops, admin)
  compute/        the LLM seam: backends (offline/openai-compat/google/
                  anthropic), prompting.py (the untrusted_data wrapper)
  crm/            one-way CRM mirror (EspoCRM adapter) — never on the send path
  db/             engine.py (the two engines/roles), models.py (schema),
                  migrate.py (idempotent migrator), sql/ (the crown jewels:
                  001 schema evolution, 002 functions, 003 triggers, 004 RLS)
  domain/         the business rules: states.py (the 33-state machine),
                  state_machine.py (the ONLY way state changes),
                  eligibility.py (the 17-check send gate), suppression,
                  approval, erasure, preflight
  evals/          golden-set checks for whichever models are configured
  guardrails/     harness.py: iteration cap, budget ceiling, tenant spend cap
  ingest/         ses_events.py (bounces/complaints), unsubscribe.py (RFC 8058)
  observability/  metrics.py, alerts.py
  pipeline/       runner.py: the per-lead control loop, sequences, recovery
  routing/        task→model-tier routing
  senders/        base.py (interface), simulated.py, ses.py, registry.py
  synthetic/      Faker prospects, including deliberately hostile ones
  workers/        send_worker.py, event_worker.py, retention_worker.py
tests/            the suite runs against real Postgres — the adversarial
                  tests are the best documentation of the guarantees
docs/             prototype-status.md (read first), control-flow.md,
                  deployment.md, phase-history.md, decisions/, img/
deploy/           production containers + GCP Terraform (README.md inside)
infra/n8n/        relay-spine.json — the scheduler workflow
scripts/          demo_journey.py, seed_synthetic.py, benchmark, evals,
                  dev_pg.sh (no-Docker local Postgres)
justfile           every command you'll ever run — skim it early
.env.example       every setting, documented; doubles as a config reference
```

Rule of thumb for "where would X be?": a *rule about what may happen*
lives in `domain/` and is mirrored in `db/sql/`; a *process that makes
things happen* lives in `pipeline/` or `workers/`; a *boundary with the
outside world* lives in `api/`, `senders/`, `ingest/`, or `compute/`.

## Part 3 — the reading path

Seven sessions, 30–60 minutes each. Don't skip the exercises — the repo
is built to be poked, and the guarantees only feel real once you've
tried to break them.

### Session 0 — run it before you read it

```bash
just sync && cp .env.example .env
just db-local-start && just db-migrate
just demo        # a synthetic lead walks every state, printed as a trace
just seed        # a 20-prospect synthetic cohort, incl. hostile ones
just api         # then open /docs, /review, /ops, /admin
just test        # the full suite against real Postgres
```

Read the `just demo` output line by line — it's the whole product in
one trace: created → … → approval gate → send gate → simulated send →
reply → booked → closed.

### Session 1 — the state machine (the heart)

Read, in order: `domain/states.py` (the enum, `_TRANSITIONS`, how error
edges are generated), `domain/state_machine.py` (the single choke point
that changes state — everything else calls this), then
[control-flow.md](control-flow.md) end to end.

*Notice:* terminal states have no outgoing edges — "do-not-contact is
permanent" is a structural fact, not a flag. *Exercise:* in `psql`, try
`UPDATE leads SET state='sent'` on a `created` lead and watch the
trigger refuse. *Afterward you can explain:* why illegal transitions
are impossible two ways — Python and a DB trigger seeded from the same
Python map.

### Session 2 — the database as enforcement

Read `db/models.py` (skim; note CHECKs and UNIQUEs), then
`db/sql/002_functions.sql` and `003_triggers.sql` slowly — every
function is a guarantee: `fn_enforce_lead_transition` (state machine +
retry cap), `fn_send_jobs_guard` (dry-run leads can't get send jobs),
`fn_auto_suppress`, `fn_dsr_erase`. Then `004_rls.sql` for forced RLS,
and `db/engine.py`: two engines, two roles — `relay` (owner,
migrations) vs `relay_app` (runtime, RLS-forced, minimal grants).

*Exercise:* connect as `relay_app` and try to DELETE an audit row.
*Afterward you can explain:* why even raw SQL under the app's own
credentials can't violate the invariants — the suite literally attacks
it that way (`tests/test_adversarial.py`, `test_dry_run.py`,
`test_tenant_isolation.py`).

### Session 3 — one pipeline tick, crash-safety, guardrails

Read `pipeline/runner.py`: `_WAIT_STATES` (the three deliberate stops),
the advance-until-you-can't loop, one-transaction-per-step, failure
parking (`error_retryable` vs `error_terminal`), `_advance_sequence`
(follow-up steps re-enter the same machine). Then
`guardrails/harness.py`: dumb counters (iterations, budget units,
tenant monthly spend) wrapping the loop *from outside* the reasoning.

*Exercise:* run `just seed`, kill Postgres mid-run
(`just db-local-stop`), restart, run again — nothing is corrupted;
recovery picks up stale work. *Afterward you can explain:* what happens
on a crash at any point, and why runaway runs die even when the clever
component is what broke.

### Session 4 — the send path (the most defended code in the repo)

Read `domain/eligibility.py` top to bottom — the 7 always-on integrity
checks and 10 real-mode checks, `DEFERRABLE_CHECKS` (pacing defers, not
blocks), the advisory-lock race-proof daily cap. Then
`workers/send_worker.py`: `FOR UPDATE SKIP LOCKED` claiming, per-job
transactions, execution-time full re-check, the thread-safe global
budget for concurrency. Then `senders/ses.py` for the last-hop
allowlist re-check at the provider boundary.

*Notice the philosophy:* approval never sends; the worker re-checks
everything at execution time because the world may have changed since
queueing. Two racing workers can't double-send because a UNIQUE
constraint — not politeness — says so (`tests/test_idempotency.py`).
*Afterward you can explain:* the five layers a real send must pass and
what each uniquely catches.

### Session 5 — the feedback loop and compliance

Read `ingest/ses_events.py` (signature-verified events → suppression in
the same transaction), `ingest/unsubscribe.py` (RFC 8058 one-click:
HMAC tokens per tenant/lead/job, master-key rotation, GET never
mutates), `domain/suppression.py`, `domain/erasure.py` (DSR: delete
everywhere, leave one hashed do-not-contact marker), and `hashing.py`
(peppered email digests + the legacy dual-lookup transition —
[decisions/email-hash-pepper.md](decisions/email-hash-pepper.md) has
the why).

*Afterward you can explain:* the full story of "someone clicks
unsubscribe" and "someone invokes GDPR erasure" — both end in
cryptographic or structural permanence.

### Session 6 — the AI layer (boring on purpose)

Read `compute/prompting.py` (escaping + `<untrusted_data>` tagging),
the backends in `compute/`, `routing/` (cheap tier vs hosted tier),
`evals/` and `synthetic/` (hostile prospects with injection attempts in
their bios). Key facts: models are configuration, not code — the
prototype ran on the Gemini API free tier (Gemini Flash for
drafting/triage, Gemma for scoring, $0 inference); anything
OpenAI-compatible (Ollama, vLLM) or the Anthropic API slots in via two
lines of `.env`; tests run on a deterministic offline stub.

*Afterward you can explain:* why prompt injection can't raise a score
or fake intent here — there's an eval that proves it, and the gates
would ignore it anyway.

### Session 7 — operations and deployment

Read `api/routes.py` once end to end (it's the product surface), then
`observability/metrics.py` + `alerts.py`,
[deployment.md](deployment.md) (manual VPS),
[../deploy/README.md](../deploy/README.md) (containers + GCP Terraform,
the two-role DSN wiring, tunnel-only ingress), and
`infra/n8n/relay-spine.json` (notice how *thin* it is — n8n is a timer,
not a brain).

*Exercise:* `just bench 2 10 4` and read what the benchmark actually
measures (reference: ~11 leads/sec full-funnel on a dev container,
offline compute).

## Part 4 — vocabulary to be fluent in

You should be able to explain each in one sentence:

- **Row-level security (forced):** Postgres filters every row by tenant
  from a session variable; FORCED means even the app role can't opt out.
- **Transactional outbox:** the send job is written in the same
  transaction as the state change, so "decided to send" and "recorded
  the decision" can't diverge; a worker executes from the table later.
- **Idempotency key:** UNIQUE(lead, step, message-version) on send
  jobs — retries and races collapse into one send at the database.
- **`FOR UPDATE SKIP LOCKED`:** concurrent workers each claim different
  rows without blocking — queue semantics inside Postgres.
- **Advisory lock:** an application-defined lock (per tenant, for the
  daily cap) so check-then-increment can't race.
- **Suppression list:** hashed do-not-contact entries written
  automatically by bounce/complaint/unsubscribe, checked by every send.
- **Dry-run invariant:** synthetic or unapproved real-person leads can
  walk the whole pipeline, but a DB trigger makes a send job for them
  impossible.
- **Two-actor send:** approval pins exact text; a separate worker
  executes later, after re-checking everything at execution time.
- **HMAC + pepper:** email digests are keyed so a leaked database
  doesn't let you dictionary-attack the suppression list; unsubscribe
  tokens are HMAC-signed per send.
- **Guardrail harness:** dumb outer limits (iterations, budget, spend)
  that keep working precisely when the clever component is what broke.

## Part 5 — design rationale FAQ

The questions every newcomer asks, answered the way the repo means them.

**Why put logic in database triggers — isn't that an anti-pattern?**
This domain's failure mode is irreversible harm (emailing someone who
opted out, leaking tenant data), so enforcement has to sit below every
possible caller — app bugs, concurrent workers, manual SQL, a confused
LLM. Business *logic* stays in Python; what lives in the DB is
*invariants* — small, testable, attacked directly by the suite. The
trade-off (harder to see in code review) is paid deliberately, and the
transition rules are seeded from one Python map so there's a single
source of truth.

**How do we know a duplicate send can't happen?**
A UNIQUE constraint on send jobs plus SKIP-LOCKED claiming plus per-job
transactions — and a test that races two workers at the same job and
asserts exactly one send. Not "we're careful"; "the database rejects
the second one."

**What happens if a process crashes mid-send?**
Every step is one transaction, so a crash rolls back to the last
committed state; the job stays claimed-but-unfinished and a recovery
pass re-queues stale work. There's a test that forces this.

**Where does the human fit?**
A reviewer approves the *exact* final text (approve/edit/reject with
rubric reasons, batch review, confidence-ordered queue). Approval never
sends — it only makes a lead eligible for a later worker pass that
re-checks all 17 rules at execution time. Every follow-up step in a
sequence gets its own approval.

**How would this scale?**
The processes are already stateless (all state in Postgres): scale the
API horizontally, raise worker concurrency (already
multi-tenant-parallel with a thread-safe budget), move compose →
Kubernetes with the same image. The real ceilings are Postgres (then:
read replicas, partitioning by tenant) and SES rate limits (pacing and
warmup ramps already exist).

**What's deliberately not done?**
[prototype-status.md](prototype-status.md) is the honest ledger: legal
artifacts, SES production access, KMS for the master key/pepper, the
human security review, a throughput target, SQS long-polling. Each is
wired to fail closed — the system refuses the associated action until
the item is done, so the list can't silently rot.

**Why n8n if it does so little?**
Exactly because it does so little: a visual timer that pokes tick
endpoints. All logic stays in versioned, tested Python; the spine is
removable (cron does the same job).

**Why Gemini — is the project locked in?**
No — models are configuration. The prototype used Gemini's free tier to
prove the funnel at $0 inference; the same seam runs Ollama/vLLM
locally or any hosted API, and tests never touch a provider.

## Part 6 — docs in reading order

1. [../README.md](../README.md) — the pitch + the three diagrams
2. [prototype-status.md](prototype-status.md) — what's real, what's
   deferred, why the list can be trusted
3. [control-flow.md](control-flow.md) — the runtime, layer by layer
4. [phase-history.md](phase-history.md) — how it was built, phase by phase
5. [decisions/](decisions/) — the decision records (sending provider,
   email-hash pepper, local tool calling); also a template for writing
   new ones
6. [deployment.md](deployment.md) then
   [../deploy/README.md](../deploy/README.md) — laptop → VPS → cloud
7. [security-review-checklist.md](security-review-checklist.md) — what
   a production gatekeeper checks
8. The `Plan` branch — the full project documentation and roadmap the
   phases were built against

## Part 7 — checkpoints

You're ramped when you can do these without the repo open:

1. Draw the lead lifecycle from memory: the main lane, the two stop
   points, the three post-send outcomes, where suppression feeds back.
2. Name the five layers a real send must pass and what each uniquely
   catches.
3. Explain to a non-engineer why "the AI can't send an email" is a
   structural claim, not a policy claim.
4. Tell the bounce story end to end: SES → SNS (signed) → SQS → event
   worker → suppression + state transition in one transaction → every
   future send to that address fails a named check.
5. Say what you'd do first to take this to production (the
   prototype-status list, in priority order, and why items 1–2 gate
   real prospects).
6. Name something you'd criticize — candidates: the admin token is a
   single static credential; alerting is webhook-or-logs, no pager
   integration; n8n's value is thin at current scale; the SQS poller
   short-polls. Knowing the warts is part of owning the system.

## Part 8 — changing the code (from one-line tweaks to full rewrites)

Understanding the system and changing it are different skills. This
part is the bridge. The one meta-rule: in this codebase, most
meaningful changes touch *pairs* of places that must stay in sync —
the recipes below name the pairs so you don't have to discover them by
breaking CI.

### 8.1 Recipes for common changes

**Add a config setting.** `config.py` (typed field, `RELAY_` prefix,
safe default) **+** a documented line in `.env.example`. If it's a
secret, also `deploy/env.prod.example` and a Secret Manager entry in
`deploy/gcp/secrets.tf`. Trap: pydantic-settings reads `.env` into the
`Settings` object, not into `os.environ` — if an external library
(like boto3) needs the variable, it must reach the real environment
(see `bootstrap.py`).

**Add a column or table.** `db/models.py` **+** a matching idempotent
`ALTER`/`CREATE` in `db/sql/001_schema_evolution.sql` (create_all only
covers brand-new tables) **+**, for a new tenant-owned table, RLS
policy and grants in `004_rls.sql`. Then `just db-migrate` (safe to
re-run) and a test that touches the new shape. Trap: forgetting 001
works on a fresh database and fails on every existing one — CI won't
catch it because CI's database is always fresh; the suite's
schema-drift test is your friend, run `just test` locally against a
migrated (not reset) DB.

**Add or change a lead state/transition.** `domain/states.py` is the
single source of truth — edit the enum and `_TRANSITIONS` only.
Migration re-seeds `lead_transition_rules`, so the DB trigger enforces
your new edge automatically after `just db-migrate`. Add the handler in
`pipeline/runner.py` if the state does work, and extend the exit-gate
journey test. Trap: editing SQL or the DB directly — the Python map
always wins on the next migrate.

**Add an eligibility check.** `domain/eligibility.py` (named check,
clear reason string) **+** decide: blocking or deferrable
(`DEFERRABLE_CHECKS` — only for pacing-style "not now" conditions)
**+** an adversarial test that attacks it, not just a happy-path one
(house convention). If the check guards something irreversible, mirror
it as a DB trigger in `002_functions.sql`/`003_triggers.sql`.

**Add an endpoint.** `api/routes.py` **+** request/response models in
`api/schemas.py` **+** the right auth dependency (tenant key vs
`require_admin`) **+** the right session: `tenant_session` for tenant
work, `admin_session` ONLY if it genuinely must bypass RLS — that
choice is a security decision (see
[security-review-checklist.md](security-review-checklist.md), step 1).
Add a test in the matching `tests/test_*.py`.

### 8.2 Rewriting whole subsystems

The architecture is deliberately "replaceable code above a load-bearing
database." Correctness does not live in any one module — it lives in
(a) the Postgres enforcement layer and (b) the test suite. That means
you can rewrite entire boxes as long as you keep their contracts, and
the contracts are short:

| Subsystem | Its contract (what the rest assumes) | Pinned by | Freely replaceable? |
| --- | --- | --- | --- |
| Compute / LLM layer (`compute/`, `routing/`) | `ComputeResult` out; untrusted text goes through the tagging seam; no side effects | offline-stub tests, `evals/`, adversarial injection tests | Yes — entire providers are config already |
| Senders (`senders/`) | `base.py` interface; last-hop recipient/allowlist re-check stays at the provider boundary | `test_phase1c_send.py`, last-hop refusal test, idempotency tests | Yes — this seam exists precisely so SES isn't structural |
| Pipeline runner (`pipeline/`) | one transaction per step; advance only along `ALLOWED_TRANSITIONS`; stop at wait states; park failures | exit-gate journey, guardrails, recovery/crash tests — and the DB trigger refuses illegal moves even if your rewrite is buggy | Yes — the state machine, not the runner, is the spec |
| Workers (`workers/`) | one-shot passes; claim via `FOR UPDATE SKIP LOCKED`; full eligibility re-check at execution | idempotency/race tests, adversarial suite | Yes |
| API (`api/`) | auth model (tenant keys + admin token); schemas; tenant vs admin session choice | endpoint tests | Yes — even swapping FastAPI is contained here |
| Review/ops/admin UI | plain HTML over the same API | — | Trivially — it's server-rendered HTML with no build step |
| Scheduler (n8n) | calls tick endpoints on a timer; zero logic | — | Trivially — cron is the documented replacement |
| CRM mirror (`crm/`) | one-way, best-effort, never on the send path | mirror tests | Yes |
| **Postgres + `db/sql/`** | **the invariants themselves** | the entire adversarial suite | **No — this is the foundation.** Replacing Postgres means reimplementing RLS, the transition trigger, suppression, and dry-run guards elsewhere, and proving them again. Treat as load-bearing. |

**The rewrite protocol** (how to replace a whole section safely):

1. **Read the contract, not the code.** For the subsystem you're
   replacing: its interface file, the DB constraints it leans on, and —
   most importantly — the tests that exercise it. The adversarial
   tests are the real spec: they encode what must stay true, not how
   it's currently done.
2. **Write the decision record first.** A dated file in
   `docs/decisions/` saying what you're replacing, why, and what the
   contract is. The three existing records show the shape.
3. **Rewrite behind the seam.** Keep the interface (or change it
   deliberately and update every caller in the same PR). The DB layer
   stays up the whole time and will refuse invalid behavior from your
   new code — that's your safety net during the rewrite, not an
   obstacle.
4. **The acceptance bar is mechanical:** `just test` fully green, and
   `just test-exit-gate` treated as non-negotiable — those tests ARE
   the product guarantees. If your rewrite needs an exit-gate test to
   change, that's a product decision, not a refactor; say so in the PR.
5. **Schema changes ride along additively** (001 evolution file,
   idempotent), and anything you deprecated gets deleted, not
   commented out — the git history is the archive.

If you honor those five steps, there is no part of `src/relay/` you
cannot rewrite from the ground up. The system was built by phases and
rewritten in places more than once already; `docs/phase-history.md`
shows several seams (sender registry, worker concurrency, the pepper
migration) that were replaced under a green suite.
