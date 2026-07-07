# Deploying RELAY to the cloud

Two layers, kept deliberately separate:

- **Layer 1 — containers** (`Dockerfile` at the repo root,
  `deploy/docker-compose.prod.yml`): provider-neutral. Runs unchanged on
  any Docker host — a GCP VM, an EC2 box, an Azure VM, a Hetzner box,
  your basement server.
- **Layer 2 — provisioning** (`deploy/gcp/`): everything GCP-specific,
  as Terraform. Swapping clouds means replacing this directory and
  nothing else (mapping table at the bottom).

The compute target is an **always-on VM**, on purpose: the send worker,
event worker, retention worker, and the n8n spine run whether or not an
HTTP request is happening, which rules out Cloud Run and every other
scale-to-zero platform. The event worker *polls* SQS, so no public
inbound webhook is needed anywhere. If RELAY ever outgrows one box, GKE
is the next step — documented as future, not built.

One architectural note to not trip over: compute is on GCP but the email
pipeline (SES → SNS → SQS) stays on **AWS**. That's intentional — RELAY
calls them as APIs over the internet, needing only outbound egress and
the AWS credentials. The manual, host-agnostic version of everything
here is [docs/deployment.md](../docs/deployment.md).

## Layer 1 — the container stack

One image (multi-stage, uv-built, non-root, Python 3.12 to match
`pyproject.toml`) serves every process; only the command differs.
`deploy/docker-compose.prod.yml` defines:

| Service | What it runs | Notes |
| --- | --- | --- |
| `migrate` | `relay-migrate`, one-shot | idempotent; every other service waits for it to complete |
| `api` | uvicorn on 8000 | bound to `127.0.0.1` only; healthchecked on `/health` |
| `send-worker` | `relay-worker --once` every `RELAY_SEND_TICK_SECONDS` (default 30s) | loop = scheduler; a failing pass kills the container so Docker's restart policy surfaces it |
| `event-worker` | `relay-events` every `RELAY_EVENTS_TICK_SECONDS` (default 300s) | polls SQS; idles politely if no queue is configured |
| `retention-worker` | `relay-retention` every `RELAY_RETENTION_TICK_SECONDS` (default daily) | purge + crash recovery |
| `n8n` | the workflow spine | reaches the API at `http://api:8000`; import `infra/n8n/relay-spine.json` once via its UI |
| `cloudflared` | Cloudflare Tunnel (profile: `tunnel`) | outbound-only ingress for the API — no public port, no load balancer |

Everything has `restart: unless-stopped`, resource limits, and — where
there's an HTTP surface — a healthcheck. There is no Mailpit (real mail
goes via SES) and **no Redis: nothing in RELAY uses it** (the original
plan reserved one; the code never grew the dependency, so provisioning
Memorystore would be paying for an empty box — see "Redis" below).

Postgres is an **external managed service**, referenced only through
`RELAY_DATABASE_URL` / `RELAY_APP_DATABASE_URL`. The two DSNs carry two
roles, wired for least privilege:

| Service | DSN(s) | Role |
| --- | --- | --- |
| `migrate` | `RELAY_DATABASE_URL` (+ `RELAY_APP_DB_PASSWORD`) | `relay` — schema owner; creates/updates `relay_app` |
| `api` | `RELAY_APP_DATABASE_URL` + `RELAY_DATABASE_URL` | `relay_app` for all tenant work; the owner session is used in code only by the admin-token endpoints (onboarding, key rotation, global suppression, preflight) |
| `send-worker`, `event-worker`, `retention-worker` | `RELAY_APP_DATABASE_URL` only | `relay_app` — RLS-forced, minimal grants; the admin DSN never enters their environment |

For a cheap first deploy with a containerized Postgres on the same box,
add the override:

```bash
docker compose -f docker-compose.prod.yml -f docker-compose.local-db.yml up -d
```

Secrets come from the environment at runtime, injected via
`--env-file` from a file assembled out of your secret store — never a
committed `.env`, never baked into the image. The full env contract is
documented in [`env.prod.example`](env.prod.example); the names are the
ones the code already reads (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_REGION`, `RELAY_SES_FROM`, `RELAY_PILOT_RECIPIENTS`,
`RELAY_SQS_QUEUE_URL`, `RELAY_ADMIN_TOKEN`, the DB URLs,
`RELAY_MASTER_KEY`, `RELAY_EMAIL_HASH_PEPPER`).

Smoke it on any Docker host:

```bash
docker build -t relay:local .
export RELAY_IMAGE=relay:local POSTGRES_PASSWORD=x RELAY_APP_DB_PASSWORD=y ...
docker compose -f deploy/docker-compose.prod.yml -f deploy/docker-compose.local-db.yml up -d
```

## Layer 2 — GCP, step by step

What Terraform provisions: a VPC with deny-by-default ingress (SSH via
IAP only), a static external IP, an e2-medium Ubuntu 24.04 VM, Cloud SQL
for PostgreSQL 16 (private IP, SSL enforced, backups + PITR), Secret
Manager secrets, an Artifact Registry repo, and a least-privilege VM
service account (per-secret accessor + registry pull + logs/metrics).

Everything defaults to **us-east4** — the closest GCP region to AWS
us-east-2, where the SES/SNS/SQS stack lives. That keeps every send and
every SQS poll a short same-coast hop instead of a transatlantic one,
and keeps prospect data hosted in the US (an EU region would drag in
GDPR data-residency scope for no reason). Override `region`/`zone` in
`terraform.tfvars` only with that trade-off in mind.

### 0. Prerequisites

`gcloud` authenticated against a project with billing, and Terraform ≥ 1.7.

### 1. Provision

```bash
cd deploy/gcp
cp terraform.tfvars.example terraform.tfvars   # edit: project, region, app_env
terraform init
terraform apply
```

Keep the Terraform state private (a GCS backend bucket is the usual
answer): the two generated database passwords live in it.

### 2. Set the operator secrets

Terraform creates the containers; you add the values:

```bash
printf '%s' "$(openssl rand -base64 48)" | gcloud secrets versions add relay-admin-token --data-file=-
printf '%s' "$(openssl rand -base64 48)" | gcloud secrets versions add relay-master-key --data-file=-
printf '%s' "$(openssl rand -base64 48)" | gcloud secrets versions add relay-email-hash-pepper --data-file=-
# AWS credentials for SES/SQS (IAM user scoped to SES send + the events queue):
printf '%s' 'AKIA…'  | gcloud secrets versions add relay-aws-access-key-id --data-file=-
printf '%s' '…'      | gcloud secrets versions add relay-aws-secret-access-key --data-file=-
# Cloudflare Tunnel token (see step 4):
printf '%s' 'eyJ…'   | gcloud secrets versions add relay-tunnel-token --data-file=-
# Optional: LLM key, n8n encryption key
printf '%s' '…'      | gcloud secrets versions add relay-google-api-key --data-file=-
```

| Secret | Required | Becomes |
| --- | --- | --- |
| `relay-admin-token` | yes | `RELAY_ADMIN_TOKEN` |
| `relay-master-key` | yes | `RELAY_MASTER_KEY` |
| `relay-email-hash-pepper` | yes | `RELAY_EMAIL_HASH_PEPPER` |
| `relay-db-password` | set by Terraform | `relay` DSN password |
| `relay-app-db-password` | set by Terraform | `RELAY_APP_DB_PASSWORD` + `relay_app` DSN |
| `relay-aws-access-key-id` / `relay-aws-secret-access-key` | for real mail | `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` |
| `relay-tunnel-token` | if `enable_tunnel` | `TUNNEL_TOKEN` |
| `relay-google-api-key` | optional | `RELAY_GOOGLE_API_KEY` |
| `relay-n8n-encryption-key` | optional | `N8N_ENCRYPTION_KEY` |

`relay-master-key` and `relay-email-hash-pepper` are long-lived: back
them up, and never rotate the pepper casually (every stored email digest
depends on it — `docs/decisions/email-hash-pepper.md`).

### 3. Build and push the image

```bash
gcloud builds submit --tag "$(terraform output -raw image)" .   # from the repo root
```

(Cloud Build; no local Docker needed. `docker build` + `docker push` to
the same tag works too.)

### 4. Create the Cloudflare Tunnel

In the Cloudflare Zero Trust dashboard: create a tunnel, add a public
hostname (e.g. `relay.example.com`) pointing at `http://api:8000`, copy
the tunnel token into the `relay-tunnel-token` secret. Put Cloudflare
Access in front of the hostname — the review/ops/admin pages and the
tenant API have no business being anonymous-public. Set
`RELAY_UNSUBSCRIBE_URL=https://relay.example.com/unsubscribe` in
`app_env` (that one route must stay publicly reachable for RFC 8058).

### 5. Boot

```bash
terraform apply   # if you changed tfvars
gcloud compute instances reset relay --zone <zone>   # re-runs the startup script
```

The startup script (every boot): installs Docker if missing → fetches
secrets into `/run/relay/relay.env` (tmpfs, mode 600 — nothing sensitive
on persistent disk) → logs into Artifact Registry with the VM's own
token → writes the compose file → `docker compose up -d`. Watch it:

```bash
$(terraform output -raw ssh)
sudo tail -f /var/log/relay-startup.log
sudo docker compose -f /opt/relay/docker-compose.yml ps
```

Then import the spine: SSH port-forward 5678
(`gcloud compute ssh relay --zone <zone> --tunnel-through-iap -- -L 5678:localhost:5678`),
open `http://localhost:5678`, import `infra/n8n/relay-spine.json`, and
activate it.

### Migrations

`migrate` runs as a one-shot compose service **before** the app services
start, on every boot and every `compose up`. It's idempotent, so that's
safe. To run one by hand:

```bash
sudo docker compose -f /opt/relay/docker-compose.yml --env-file /run/relay/relay.env run --rm migrate
```

**The Cloud SQL role gotcha, handled:** RELAY uses two roles — `relay`
(owner, migrations) and the RLS-constrained `relay_app`, which
`migrate.py` creates via `CREATE ROLE`. Cloud SQL has no true superuser,
but users created through its API (like `relay`) are members of
`cloudsqlsuperuser`, which includes `CREATEROLE` — sufficient. The
migration path was checked for superuser assumptions and has none (no
`CREATE EXTENSION`, no `ALTER SYSTEM`; `FORCE ROW LEVEL SECURITY` needs
only table ownership, and `relay` owns the tables). The stock migrator
runs unmodified. SSL is enforced on the instance (`ENCRYPTED_ONLY`) and
required in both DSNs (`sslmode=require`).

### Redis

Not provisioned. Nothing in `src/relay/` or the dependency tree uses
Redis today, so Memorystore would be a bill for an empty box. n8n is
pinned to single-main mode (`EXECUTIONS_MODE=regular` in the compose
file), which needs no Redis either — **the one condition that changes
this is switching n8n to queue mode** (`EXECUTIONS_MODE=queue`, for
multi-worker n8n at scale), which requires a Redis broker. If that day
comes, add Memorystore in `deploy/gcp/` — or run a Redis container on
the VM as the cheap fallback (one more service in the compose file).

### Worker pacing

The tick workers are one-shot commands wrapped in explicit sleep loops
inside their containers — never a bare container-restart spin. The
intervals are env-tunable without an image rebuild:

| Variable | Default | Governs |
| --- | --- | --- |
| `RELAY_SEND_TICK_SECONDS` | 30 | gap between send-worker passes |
| `RELAY_EVENTS_TICK_SECONDS` | 300 | gap between SQS drain passes |
| `RELAY_RETENTION_TICK_SECONDS` | 86400 | gap between retention/purge passes |

A failing pass still exits non-zero and kills its container, so real
breakage surfaces as a visible restart-loop with backoff rather than a
silent stall. One known src-level improvement (out of scope for this
infra change): the SQS poller currently calls `receive_message` with
`WaitTimeSeconds=0` (short polling); switching it to ~20 would make each
drain pass long-poll. At the default 300s idle interval the request
volume is tiny either way (~12 passes/hour, well inside the SQS free
tier), so the interval, not long-polling, is what bounds cost today.

### Networking summary

| Direction | What | How |
| --- | --- | --- |
| Ingress | API for reviewers/operators | Cloudflare Tunnel (outbound-only `cloudflared`), Cloudflare Access in front |
| Ingress | SSH | IAP only (`35.235.240.0/20`); no public SSH |
| Ingress | everything else | denied — no public ports at all |
| Egress | AWS SES + SQS (cross-cloud, deliberate), Gemini API, Cloudflare edge, Artifact Registry | default-allow egress; static IP if you need to allowlist it |
| Internal | VM ↔ Cloud SQL | private IP via VPC peering, SSL enforced |

## Porting to another cloud

Layer 1 moves as-is. Layer 2 is a directory swap — the same five
ingredients on any provider:

| Ingredient | GCP (built) | AWS equivalent | Azure equivalent |
| --- | --- | --- | --- |
| Always-on VM + startup script | GCE + metadata startup script | EC2 + user data | Azure VM + custom data / cloud-init |
| Managed Postgres 16, private + SSL | Cloud SQL | RDS for PostgreSQL | Azure Database for PostgreSQL Flexible Server |
| Secret store, fetched at boot | Secret Manager (VM identity token) | Secrets Manager (instance profile) | Key Vault (managed identity) |
| Image registry | Artifact Registry | ECR | ACR |
| Locked-down ingress | firewall + IAP SSH + Cloudflare Tunnel | security group + SSM Session Manager + same tunnel | NSG + Bastion + same tunnel |

The startup script's only provider-specific parts are the two `curl`
calls (metadata token, secret access) and the registry login; everything
below the env-file assembly is identical. An AWS deploy on EC2 is
arguably even simpler: SES/SQS become same-cloud and the IAM user
credentials can be dropped for an instance role. If everything moves to
one cloud someday, that credential simplification is the main win.

## Scaling beyond one box (not built)

GKE (or any Kubernetes) is the path when one VM stops being enough: the
image is already stateless, migrations are idempotent (a Job), the
workers are Deployments or CronJobs, and the API scales horizontally
because all state is in Postgres. Nothing in Layer 1 would change except
replacing compose with manifests.
