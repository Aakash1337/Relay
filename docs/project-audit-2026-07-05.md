# Whole-project audit — 2026-07-05

> **Revalidated 2026-07-09 at commit `23460d0`.** The original audit is
> preserved below. See [Reassessment — 2026-07-09](#reassessment--2026-07-09)
> for the current finding status, deployment findings, and executed offline
> and online functionality results.

## Scope and method

This audit reviewed the repository structure, configuration, API boundary, database/RLS design, send path, ingestion path, tests, and operational documentation for RELAY as of 2026-07-05.

Commands run:

- `git status --short`
- `find .. -name AGENTS.md -print`
- `rg --files -g '!node_modules' -g '!vendor'`
- `uv run ruff check .`
- `uv run pytest -q`
- `uv run pylint src/relay --score=n`
- `uv run python -m bandit -r src -q`

## Executive summary

RELAY is architected with unusually strong safety boundaries for an outbound automation prototype: tenant isolation is pushed into PostgreSQL RLS, send execution is separated from approval, real sending is fail-closed by default, API keys are hashed, email digests are peppered, one-click unsubscribe is tokenized, and SES ingestion validates SNS signatures before mutating state.

The main project risk is not a missing top-level safety concept; it is operational hardening and CI reproducibility. The local audit environment did not have the expected PostgreSQL test database running, so the full pytest suite could not execute here. Static checks passed for Ruff, while Pylint surfaced a mix of expected SQLAlchemy false positives and maintainability warnings that should be triaged into either configuration suppressions or refactors.

## Strengths observed

- **Tenant isolation is a first-class invariant.** The code uses separate admin and app engines, tenant-pinned app sessions, and SQL RLS policies rather than relying only on application-layer filters.
- **Real sending is deliberately hard to enable.** The default sender provider is `none`, real sends are disabled by default, pilot recipients are fail-closed when empty, and the worker owns the actual send boundary.
- **PII minimization is consistently represented.** Email addresses are canonicalized, peppered with HMAC-SHA256, and logs use digests rather than raw addresses in provider-event paths.
- **Compliance events are decoupled from lead-state transitions.** Hard bounces, complaints, and unsubscribes write suppression entries even when the lead cannot transition, which is the right safety posture.
- **Destructive tests contain a database-name guard.** The test fixture refuses to run reset migrations unless both configured database names visibly look like test databases.

## Findings

### A1 — Full test suite is not reproducible unless PostgreSQL is running on the expected port

**Severity:** Medium  
**Category:** CI / developer experience / release confidence

The pytest suite requires a real PostgreSQL instance and defaults to `127.0.0.1:5433/relay_test`. In this audit environment, `uv run pytest -q` emitted setup errors across the suite because the database service was unavailable. This is an environment limitation, not necessarily a product defect, but it means a fresh checkout cannot validate the project with a single test command unless the developer already knows to start the database dependency.

**Recommendation:** Add a documented one-command test path, for example `just test-db-up && just test`, or make the default `just test` bring up the compose database and run migrations before pytest. CI should publish a clear failure if PostgreSQL is unavailable rather than generating hundreds of repeated setup errors.

### A2 — Pylint is configured but not currently clean

**Severity:** Low to Medium  
**Category:** maintainability / static analysis

`uv run pylint src/relay --score=n` reported multiple categories:

- SQLAlchemy false positives such as `func.count is not callable`.
- Maintainability warnings such as very large functions/modules and many positional arguments.
- A potentially confusing exception-handler warning in `ratelimit.with_backoff` because the caught exception tuple is built dynamically.
- Minor hygiene warnings including import-outside-toplevel, reimport, line length, and unspecified file encoding.

Some warnings are intentional design tradeoffs, but leaving them unsuppressed makes Pylint less useful as a gate because real regressions are mixed with known noise.

**Recommendation:** Decide whether Pylint is a gate. If yes, add targeted suppressions for SQLAlchemy and intentional lazy imports, then refactor the remaining high-signal warnings. If no, remove it from the advertised dev checks or document it as advisory-only.

### A3 — Bandit is not installed despite the project having security-sensitive code paths

**Severity:** Low  
**Category:** security tooling

`uv run python -m bandit -r src -q` failed because Bandit is not in the development dependency group. Given this project handles API keys, unsubscribe tokens, webhooks, provider certificates, and deletion/DSR flows, a lightweight security scanner would be valuable as an advisory CI job.

**Recommendation:** Add Bandit (or an equivalent scanner) to the dev toolchain with a project baseline. Keep it advisory at first to avoid blocking on low-value findings, then promote high-signal rules to CI enforcement.

### A4 — Production secret defaults are intentionally marked as dev, but there is no process-level production guard

**Severity:** Medium  
**Category:** operational security

The configuration defaults include dev master-key and email-pepper values. The comments clearly state these are not production values, but the settings layer itself does not prevent `RELAY_ENV=prod` from starting with these defaults. A production deployment with missing secret injection could therefore boot with known development cryptographic material.

**Recommendation:** Add a startup/settings validation that rejects production-like environments when `master_key`, `email_hash_pepper`, database passwords, or admin token settings are default/empty. This should fail fast before serving API traffic or running workers.

### A5 — SNS signature verification pins the SNS host but relies on the fetched certificate contents

**Severity:** Low to Medium  
**Category:** webhook authentication hardening

The SES/SNS ingestion path properly rejects non-HTTPS and non-SNS-host certificate URLs, which closes a common SSRF/self-signed-cert bypass class. The verifier then loads the fetched PEM and verifies the message signature using its public key. This is strong against ordinary attacker-controlled URLs, but the implementation does not explicitly validate the certificate chain or certificate identity beyond URL host pinning.

**Recommendation:** Consider using AWS's documented SNS message validation approach end-to-end, including certificate-chain validation behavior from the TLS/client layer or a vetted library. At minimum, document the trust assumption: HTTPS to an AWS SNS host plus a valid signature under the fetched certificate.

### A6 — API route module is approaching a maintainability cliff

**Severity:** Low  
**Category:** maintainability

The API routes module has grown beyond Pylint's default module-size threshold. This is not immediately unsafe, but it increases review cost around security-sensitive endpoints because tenant routes, admin routes, worker ticks, webhook ingestion, UI pages, metrics, DSR, and preflight flows share one file.

**Recommendation:** Split route registration by concern: tenant CRUD/campaigns, review/approval, admin/onboarding/preflight, webhooks/unsubscribe, observability/economics, and internal worker endpoints. Keep dependencies and auth requirements close to each router.

## Suggested next steps

1. Make the database-backed test path one-command and CI-verifiable.
2. Add production settings validation for known dev/default secrets.
3. Triage Pylint into actionable warnings versus intentional suppressions.
4. Add an advisory security scanner baseline.
5. Split `api/routes.py` by route domain before adding more endpoints.

## Verification status

- Ruff: passed.
- Pytest: blocked by unavailable PostgreSQL test service in this environment.
- Pylint: completed with findings.
- Bandit: blocked because the tool is not installed in the dev environment.

---

## Reassessment — 2026-07-09

### Scope and baseline

This reassessment reviewed the original findings against commit `23460d0`
and examined the newly added container image, production Compose topology,
GCP Terraform, startup path, CI workflow, deployment documentation, and
onboarding material. The update added 23 commits and changed 33 files.

Checks performed:

- `git status -sb` and `git log`/`git diff` against the previous local head
- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run pylint src/relay --score=n`
- `uv run pytest tests/ -q` plus a fail-fast database diagnostic
- `uv run python -m bandit -r src -q`
- Rendered production Compose configuration with non-secret placeholder values
- Static review of the Dockerfile, production Compose files, Terraform,
  startup script, CI workflow, and deployment documentation

The local pytest run could not complete because neither expected PostgreSQL
port was available and Docker Desktop was not running. This is recorded as an
environment limitation; the CI workflow now supplies PostgreSQL and runs the
full suite.

### Current executive summary

The architectural safety assessment remains positive. The update materially
improves CI, deployment documentation, containerization, database-role
separation, private ingress, managed-database posture, backups, and onboarding.
Ruff remains clean. No original finding is fully obsolete, however: A1 and A4
are partially mitigated, while A2, A3, A5, and A6 remain open.

The most important new risks are in the production deployment contract. The
Compose allowlist silently drops supported settings, all workers inherit a
broad shared set of secrets, and Docker's independent restart behavior does
not provide the documented migrate-before-runtime ordering on host reboot.
These are deployment-hardening issues, not reasons to discount the strong
application-level safety model, but they should be addressed before treating
the new cloud path as production-ready.

### Status of the original findings

| Finding | Current status | Evidence after update |
| --- | --- | --- |
| A1 — PostgreSQL-backed test reproducibility | **Partially resolved** | CI now provisions PostgreSQL 16 and runs the full suite. Local `just test` still only invokes pytest, whose default is `127.0.0.1:5433/relay_test`; the documented Docker database exposes `5432` and creates `relay`, so the local Docker path is not automatically wired to the tests. |
| A2 — Pylint is not clean | **Open** | Pylint remains part of `just lint` and still exits non-zero with SQLAlchemy false positives, the dynamic exception-handler warning, module-size/complexity findings, and smaller hygiene warnings. |
| A3 — Bandit is absent | **Open** | Bandit is still not in the development dependencies or CI; invoking it through the project environment fails with `No module named bandit`. |
| A4 — No process-level production-secret guard | **Partially resolved** | Production Compose and the GCP startup path require several secrets, which protects those deployment paths from missing values. `Settings` itself still accepts `RELAY_ENV=prod` with known development master-key, pepper, and database defaults, no admin token, and no production validator. |
| A5 — SNS signing-certificate trust | **Open** | The verifier still host-pins the certificate URL and verifies the message with the fetched key, but it does not validate the certificate issuer, chain, validity period, or identity. |
| A6 — Monolithic API routes | **Open** | `src/relay/api/routes.py` is 1,033 lines with 33 route decorators, spanning tenant/admin, review, observability, DSR/preflight, SES/unsubscribe, and worker-tick concerns in one router. |

### New findings from the deployment update

#### A7 — Production deployment silently drops supported settings

**Severity:** High
**Category:** configuration correctness / send controls / operability

The GCP startup script writes the Terraform `app_env` map into the Compose
environment file, but `docker compose --env-file` only supplies interpolation
values. The production Compose file then explicitly allowlists which variables
enter each container. That allowlist omits 31 settings supported and documented
by the application, including hourly and warmup pacing, rate limits, bounce
thresholds, recovery timeout, CRM configuration, fit threshold, and cost
calibration.

An operator can therefore set a supported value in `app_env` and see a
successful deployment while the container silently uses its code default. In
particular, production pacing can remain disabled and CRM can remain `none`
despite apparently valid deployment configuration.

**Recommendation:** Define complete, service-specific environment mappings and
add a CI contract test that compares the deployment surface with the `Settings`
model. Reject unknown or unsupported deployment keys instead of ignoring them.

#### A8 — Workers receive unrelated high-value secrets

**Severity:** High  
**Category:** least privilege / credential exposure

The shared `x-app-env` anchor gives the send, event, and retention workers the
admin token, master key, email pepper, AWS credentials, LLM credentials, SES
webhook token, and alert webhook. Rendered Compose output confirmed that every
worker receives the entire shared set. No worker needs the admin token, and the
retention and event workers receive several provider credentials unrelated to
their responsibilities.

A compromise in one narrowly scoped worker can therefore expose credentials
for unrelated systems, including an admin credential capable of calling
privileged API endpoints.

**Recommendation:** Split the shared environment into a minimal common base and
per-service secret sets. Document and test the required variables for each
service in rendered Compose output.

#### A9 — Host reboot bypasses the documented migration ordering

**Severity:** High  
**Category:** deployment correctness / upgrade safety

Runtime services use `restart: unless-stopped`, while migration is a one-shot
container with `restart: "no"`. When Docker starts after a host reboot, restart
policies can restore existing runtime containers independently; Compose
`depends_on` ordering applies when Compose performs an operation, not when the
Docker daemon applies restart policies. The metadata startup script runs
`docker compose up` only later.

This creates a window in which the old API and workers can run with the old
image and environment before the documented per-boot migration, secret refresh,
image pull, and Compose reconciliation occur.

**Recommendation:** Use a system service or equivalent ordered boot unit that
stops the previous runtime stack, refreshes configuration, pulls the selected
image, runs migration successfully, and only then starts runtime services.

#### A10 — Floating artifacts make reboot an unreviewed upgrade

**Severity:** Medium to High  
**Category:** supply-chain integrity / reproducibility / rollback

Production defaults the RELAY application image to `latest`; n8n and
cloudflared also use `latest`, and the GCP startup script pulls images on every
boot. The Dockerfile copies `uv:latest`, while Terraform's dependency lock file
is ignored. A routine reset can therefore introduce unreviewed upstream changes
or persistent-data migrations and make rollback difficult.

**Recommendation:** Deploy the application by commit SHA or immutable digest,
pin n8n, cloudflared, build images, and CI actions to reviewed versions or
digests, and commit `.terraform.lock.hcl`. Update pins through an explicit,
reviewed dependency process.

#### A11 — Tmpfs does not keep all secrets off persistent disk

**Severity:** Medium
**Category:** secrets management / documentation accuracy

The deployment documentation says the tmpfs environment file means no secret
reaches persistent disk. Tmpfs does protect the source file, but Compose injects
those values as container environment variables. Docker retains container
configuration, including its environment, in the Docker data root; privileged
users can also retrieve it through container inspection. The VM boot disk,
snapshots, Docker metadata, and root access must therefore be treated as
secret-bearing.

**Recommendation:** Correct the threat model and backup guidance. If strict
ephemeral handling is required, add file-based secret support and mount
service-specific secret files instead of placing credentials in container
environment metadata.

#### A12 — The documented Cloud Build command has no valid stated working directory

**Severity:** Medium  
**Category:** deployment runbook correctness

The provisioning steps first change into `deploy/gcp`. The documented build
command then needs Terraform output from that directory but also uses `.` as the
Docker build context and says to run from the repository root. From
`deploy/gcp`, the build context lacks the root Dockerfile and source; from the
repository root, plain `terraform output` cannot find the deployment state.

**Recommendation:** Use a command with an explicit Terraform working directory
from the repository root, for example:

```bash
gcloud builds submit --tag "$(terraform -chdir=deploy/gcp output -raw image)" .
```

#### A13 — Hosted-model JSON output is nondeterministic in live use

**Severity:** High
**Category:** online functionality / model contract / resumability

The configured `gemini-3.5-flash` backend did not reliably honor the
single-JSON-object contract even though the request asks for JSON response mode.
The first hosted eval run failed the release threshold at 7/8 because the
conservative sparse-scoring response contained extra data after the JSON object;
an immediate rerun passed 8/8. A later live pipeline failed in outreach-copy
generation with the same `Extra data` parse error. Resuming that exact lead
then succeeded and the journey completed.

The failure is transactionally safe—the step rolls back and the lead remains
resumable—but `ComputeOutputInvalid` bubbles out of the runner instead of being
parked in a named error state. This makes online behavior flaky and forces the
operator or scheduler to discover that a retry is appropriate.

**Recommendation:** Treat invalid structured output as an explicit bounded
reformat/retry path or a named retryable/terminal state, record the raw response
securely for diagnostics, and run hosted-model evals repeatedly in the release
gate. Do not promote this model/prompt combination while a single eval pass can
alternate between 87.5% and 100%.

#### A14 — The operator demo crashes on a valid rejected lead

**Severity:** Medium
**Category:** developer experience / functional correctness

With live Gemma scoring and the default fit threshold, the synthetic demo lead
legitimately ended at `scored_rejected`. `scripts/demo_journey.py` nevertheless
assumed an outreach draft existed and called `scalar_one()`, raising
`NoResultFound`. The pipeline behaved correctly and safely; the demonstration
script did not handle a valid terminal branch.

**Recommendation:** Make the demo deterministic by pinning an offline backend or
known score, or branch on `RunOutcome.final_state` and explain rejected versus
qualified outcomes without querying for a nonexistent draft.

### Executed functionality audit

The earlier assessment was primarily static because PostgreSQL was unavailable.
This follow-up started Docker Desktop, created a disposable PostgreSQL 16
database on `127.0.0.1:5433`, and executed the product rather than inferring its
behavior. No development database was touched, and all offline sends used the
simulated provider.

#### Offline execution results

| Surface | Result | Evidence |
| --- | --- | --- |
| Full PostgreSQL suite | **Pass** | All 331 tests passed. The normal host run produced 330 passes and one client-tool skip; the backup/restore erasure test then passed separately with matching PostgreSQL 16 `pg_dump`/`psql`. |
| Coverage | **Pass with gaps** | 92% total statement coverage (3,660 statements, 308 missed). Lowest material modules included CRM Espo at 52%, Anthropic at 70%, event worker at 73%, sender registry at 74%, and send worker at 84%. |
| Demo journey | **Pass offline** | Twenty state transitions completed through approval, simulated sending, interested reply, booking, and closure. |
| Synthetic cohort | **Pass** | Twenty prospects were seeded, approved, simulated-sent, replied, and converged: 10 unsubscribed, 6 not interested, 4 closed. |
| API | **Pass** | Health, onboarding, lead creation, pipeline runs, approval, worker tick, trace, economics, metrics, and OpenAPI documentation were exercised over a live local server. |
| Browser UI | **Pass** | Review queue load and approval, operations metrics refresh, and admin tenant onboarding all succeeded with no browser-console errors. |
| Workers | **Pass offline** | Send, retention, and event-worker no-queue paths completed; send execution remained simulated. |
| Reasoning evals | **Pass offline** | Both offline tiers scored 100%. |
| Throughput | **Measured** | Two tenants × ten leads at concurrency four completed at 10.6 leads/sec end to end on this machine. |
| Container image | **Pass** | Production image built; application imports and worker/uvicorn entrypoints resolved as the non-root runtime image. |
| n8n | **Pass at artifact/runtime boundary** | Workflow JSON parsed as five nodes and three connections; the current floating image started and reported n8n 2.29.9. The workflow was not connected to external services. |
| Compose/Terraform | **Pass at validation boundary** | Both production Compose variants rendered; Terraform format, init, validate, and startup-template rendering passed. No cloud resources were applied. |
| Static/security scans | **Mixed** | Ruff and formatting passed; Pylint remained non-zero. Bandit found zero high, three medium, and seven low alerts (the medium results were generated-template/parameterized-SQL heuristics and SNS-required SHA-1 compatibility). `pip-audit` found no known vulnerabilities in locked runtime dependencies. |

#### Online execution results

| Integration | Result | Evidence / boundary |
| --- | --- | --- |
| Google Gemma workhorse | **Pass** | `gemma-4-31b-it` passed 8/8 live eval cases and successfully performed classification, enrichment, scoring, and reply triage in a live journey. |
| Google Gemini orchestrator | **Flaky / not release-ready** | One eval run scored 7/8, the next 8/8; a live outreach-copy step also failed once with extra data after JSON, then succeeded when the same lead resumed. See A13. |
| Live-model pipeline | **Pass after resume** | With the threshold temporarily set to zero to force the qualified branch, the same lead resumed after the Gemini output failure, reached approval, simulated send, live Gemma triage, booking, and `closed`. |
| AWS identity / SES / SQS | **Pass at authentication/send boundary; observability incomplete** | STS authentication succeeded, the configured SQS queue was reachable, and SES accepted the authorized `SendEmail` requests on 2026-07-09 and 2026-07-13. The narrowly scoped IAM user still denied SES account, identity, DKIM, suppression, notification, SNS-subscription, and CloudWatch inspection. Repeated post-send SQS polls returned no delivery, bounce, delay, complaint, or rejection event even though both 2026-07-13 messages ultimately arrived, so the SES → SNS → SQS outcome path remains unverified or misconfigured. |
| Real email | **Pass end to end; Gmail placement warning** | On 2026-07-13, two fresh authorized, allowlisted real-mode jobs—one per controlled pilot inbox—each passed all 18 execution-time eligibility checks. Each one-shot worker reported `sent=1` with zero blocks, failures, deferrals, or errors; both jobs and leads persisted as `sent` with provider message IDs and `send.executed` audit records. The operator confirmed that the Cybic message reached the inbox and the Gmail message arrived in Spam. This proves transport to both destinations but not reliable Gmail inbox placement. The local `.env` master switch remained false at rest and was enabled only inside each one-shot sending process. |
| GCP / Cloudflare / EspoCRM | **Not executed live** | Terraform and configuration were validated only. No GCP project, Cloudflare token, or live EspoCRM credentials were available in this audit. |

#### Credential-handling note

Credentials supplied through a conversational channel should be treated as
exposed and rotated. The local `.env` remained ignored by Git and was not added
to any commit. Future online audits should load narrowly scoped credentials
directly from the local secret store without transmitting them in chat.

### Updated priorities

1. Stabilize structured output from the hosted model and explicitly handle
   `ComputeOutputInvalid` in the pipeline.
2. Repair and verify SES outcome telemetry, then investigate Gmail spam
   placement before treating pilot deliverability as release-ready.
3. Fix the production configuration contract and add a rendered-config test.
4. Reduce each service's secret set to the minimum it needs.
5. Enforce migration-before-runtime ordering across host reboots and upgrades.
6. Pin production/build artifacts and commit the Terraform provider lock file.
7. Add process-level production validation for known development secrets.
8. Make the local PostgreSQL test path match the documented one-command flow.
9. Fix the demo's rejected-lead branch and raise coverage in external adapters
   and worker entrypoints.
10. Triage Pylint, add Bandit and dependency auditing to CI, and split the route
   module.
11. Correct the secret-at-rest and Cloud Build runbook documentation.

### Current verification status

- Ruff lint and format: passed (111 files formatted).
- Pytest: all 331 tests executed and passed across the host run plus the matching
  PostgreSQL-client backup/restore run.
- Coverage: 92% total.
- Pylint: completed with exit 30 and the finding classes described in A2/A6.
- Bandit: independently executed through `uvx`; zero high, three medium, seven
  low. It remains absent from the project dependency group and CI.
- Dependency audit: no known vulnerabilities found in locked runtime packages.
- Offline demo, seed cohort, API, review/ops/admin UI, workers, evals, benchmark,
  image build, n8n artifact/runtime boundary, Compose, and Terraform validation:
  executed as described above.
- Online Google model evals and a live-model pipeline: executed; Gemma passed and
  Gemini showed the nondeterministic contract failure in A13.
- AWS authentication, SQS access, and real SES sending: executed. The
  2026-07-13 follow-up sent exactly one controlled message to each pilot
  address; both were received, with Cybic inbox placement and Gmail Spam
  placement. RELAY's persisted sent state was verified for both. No SES outcome
  event reached SQS during the observation windows, so delivery telemetry is a
  remaining operational gap despite confirmed transport.
- GCP, Cloudflare, and live EspoCRM deployment: not executed because the
  required external infrastructure or credentials were unavailable; these are
  explicit remaining boundaries, not claimed passes.
