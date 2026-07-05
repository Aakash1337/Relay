# Deploying RELAY

How to run RELAY somewhere that isn't your laptop. The target here is
the simplest real deployment — one VPS, one Postgres — because that's
what this prototype is sized for. Nothing in the design fights a bigger
setup (all state lives in Postgres; the processes are stateless), but
nobody has written the Kubernetes chapter and this doc won't pretend
otherwise.

The README's "Getting started" is the *development* setup: auto-reload,
throwaway database, mail caught by Mailpit. This doc is what changes
when the thing has to stay up.

## The shape of it

One codebase, four processes, one database. There is no build step and
no compiled artifact: a deployment is a checkout, a virtualenv, and a
`.env`.

| Process | Command | Cadence | What it does |
| --- | --- | --- | --- |
| API | `uv run uvicorn relay.api.app:app --host 127.0.0.1 --port 8000` | long-running | routes, review/ops/admin pages, unsubscribe, webhooks |
| Send worker | `uv run relay-worker --once --concurrency 4` | every ~5 min | claims queued send jobs, re-checks every rule, sends |
| Event poller | `uv run relay-events` | every ~5 min | drains the SQS queue: bounces/complaints → suppression |
| Retention | `uv run relay-retention` | daily | purges leads past their retention date |

The three workers are deliberately one-shot commands: scheduling is the
deployment's job, not the code's. Anything that can run a command on a
timer works — cron is fine, systemd timers are fine, the n8n spine is
fine. Overlapping runs are safe (job claiming uses
`FOR UPDATE SKIP LOCKED`), so a slow pass never corrupts anything; it's
just wasted work.

The send worker can also be driven over HTTP instead of cron:
`POST /internal/send-worker/tick` (admin token required) runs one pass.
That's what the shipped n8n workflow does every five minutes, after a
`/health` check. Pick cron **or** the spine for this — running both is
harmless but pointless.

## Ports

| Port | What | Exposure |
| --- | --- | --- |
| 8000 | RELAY API (uvicorn) | private; public only via the reverse proxy |
| 443 | reverse proxy (nginx/Caddy) → 8000 | **the only public port** |
| 5432 | PostgreSQL 16 | private |
| 5678 | n8n, if you use it as the scheduler | private |
| 11434 | Ollama, only if a model tier is local | private |
| 8025 / 1025 | Mailpit | dev only — don't deploy it |

Two routes genuinely need to be reachable from the internet, and both
arrive through the proxy on 443:

- `GET|POST /unsubscribe` — RFC 8058 one-click lives here. Real mail
  carries this URL in its headers, so it must be public HTTPS, and
  `RELAY_UNSUBSCRIBE_URL` must be set to the public form of it.
- `POST /webhooks/ses` — only if you choose SNS push over SQS polling
  for bounce/complaint events (see below).

Everything else — the review queue, ops dashboard, admin console, the
whole tenant API — has no business being public. Keep it behind the
proxy's allowlist, a VPN, or basic auth at the proxy; the app's own
auth (tenant keys, admin token) is a second layer, not the only one.

## Step by step

1. **Postgres 16.** A managed instance or a local install both work.
   Create a `relay` database and a superuser-ish role for migrations.
   The app itself connects as `relay_app`, a non-superuser role that
   the migration creates and that is subject to forced row-level
   security — that split is load-bearing, don't collapse it.

2. **Code and dependencies.**

   ```bash
   git clone <repo> && cd relay
   uv sync --no-dev          # runtime deps only
   ```

3. **Configuration.** `cp .env.example .env`, then change every value
   that says it's a dev value. The non-negotiables:

   - `RELAY_DATABASE_URL` (admin, migrations) and
     `RELAY_APP_DATABASE_URL` + `RELAY_APP_DB_PASSWORD` (runtime)
   - `RELAY_ADMIN_TOKEN` — long and random; it guards onboarding,
     ticks, and the admin console
   - `RELAY_MASTER_KEY` and `RELAY_EMAIL_HASH_PEPPER` — long, random,
     and backed up. Losing the pepper orphans every suppression digest;
     the go-to-production plan puts both in a KMS
     (docs/prototype-status.md).
   - `RELAY_ENV=prod`
   - one compute backend per tier (`RELAY_COMPUTE_*`) and its API key,
     or a reachable OpenAI-compatible server

   Two facts about how configuration is read, both of which bite:

   - The app reads `.env` **from the process working directory**. Run
     everything from the repo root, or export the variables into the
     process environment instead.
   - Real environment variables **win over `.env`**. That's the
     correct precedence, with one trap: boto3 reads the standard
     `AWS_*` names from the environment, so if your host or supervisor
     injects its own `AWS_*` values, those are the credentials that
     send mail. When in doubt, export explicitly:
     `set -a; . ./.env; set +a` before the command.

4. **Migrate.** `uv run relay-migrate` — idempotent, safe to re-run on
   every deploy. It applies schema evolution, triggers, RLS, and
   re-seeds the transition rules.

5. **Run the API under a supervisor.** Any process manager works; a
   systemd unit is the classic shape:

   ```ini
   [Unit]
   Description=RELAY API
   After=network.target postgresql.service

   [Service]
   User=relay
   WorkingDirectory=/opt/relay
   ExecStart=/usr/local/bin/uv run uvicorn relay.api.app:app --host 127.0.0.1 --port 8000
   Restart=on-failure

   [Install]
   WantedBy=multi-user.target
   ```

   No `--reload` in production. The API is stateless, so
   `--workers 2` (or several instances behind the proxy) is safe if
   one process ever becomes the bottleneck.

6. **Schedule the workers.** Crontab for the `relay` user:

   ```cron
   */5 * * * *  cd /opt/relay && set -a && . ./.env && set +a && uv run relay-worker --once --concurrency 4
   */5 * * * *  cd /opt/relay && set -a && . ./.env && set +a && uv run relay-events
   10 3 * * *   cd /opt/relay && set -a && . ./.env && set +a && uv run relay-retention
   ```

   If you'd rather have the visual scheduler, import
   `infra/n8n/relay-spine.json` into an n8n instance, give it
   `RELAY_API_BASE_URL` and `RELAY_ADMIN_TOKEN`, and drop the first
   cron line. The spine currently drives only the send worker;
   events and retention stay on cron either way.

7. **Reverse proxy + TLS.** Terminate HTTPS at nginx or Caddy, forward
   to `127.0.0.1:8000`, and restrict everything except `/unsubscribe`
   and (if used) `/webhooks/ses` to trusted sources. Set
   `RELAY_UNSUBSCRIBE_URL=https://your-domain/unsubscribe`.

8. **AWS, when real mail is on the table.** In SES: verify the sending
   domain, set up SPF/DKIM/DMARC, create a configuration set whose
   events go to an SNS topic. For the return path, pick one:

   - **SQS polling** (what the pilot used): subscribe an SQS queue to
     the topic, set `RELAY_SQS_QUEUE_URL`, and let `relay-events` drain
     it. Nothing needs to be publicly reachable.
   - **SNS push**: subscribe `https://your-domain/webhooks/ses?token=…`
     and set `RELAY_SES_WEBHOOK_TOKEN`. One less process, one more
     public endpoint. Signatures are verified either way.

   Then the sender settings: `RELAY_SENDER_PROVIDER=ses`,
   `RELAY_SES_FROM`, the attestation flags, and the allowlist. Real
   sending stays structurally off until `RELAY_REAL_SEND_ENABLED=true`
   **and** the provider is configured **and** the attestations are
   true **and** (in the sandbox) the recipient is on
   `RELAY_PILOT_RECIPIENTS`. That stack of independent switches is the
   point; resist the urge to shortcut it.

9. **Smoke it.** `curl https://your-domain/health`, onboard a tenant
   via `POST /internal/tenants/onboard`, run `just demo` against the
   production database if you want to see a full dry-run journey, and
   check `/ops` renders metrics.

## Upgrades

```bash
git pull
uv sync --no-dev
uv run relay-migrate     # idempotent; new columns arrive via db/sql/001
systemctl restart relay-api
```

Schema changes ship as idempotent SQL (`db/sql/001_schema_evolution.sql`),
so there is no migration-version bookkeeping to get wrong; re-running
the migrator is always the right move.

## What this doc is not

This is a prototype's deployment guide, not a production runbook. The
gap between the two is written down in
[prototype-status.md](prototype-status.md): SES production access, a
KMS for the master key and pepper, the legal artifacts, a security
review, and a load test on production-shaped hardware. None of that is
wiring — it's operator work — but a deployment that skips it is a
pilot, not a product.
