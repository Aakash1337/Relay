# List all available commands
default:
    @just --list

# ── Dependencies ────────────────────────────────────────────────────────────

# Install / sync all dependency groups
sync:
    uv sync --all-groups

# Add a runtime dependency  (usage: just add fastapi)
add package:
    uv add {{package}}

# Add a dev-only dependency  (usage: just add-dev pytest)
add-dev package:
    uv add --group dev {{package}}

# Upgrade all locked dependencies to their latest allowed versions
update:
    uv lock --upgrade
    uv sync --all-groups

# ── Linting & Formatting ────────────────────────────────────────────────────

# Run ruff linter
lint-ruff:
    uv run ruff check .

# Run pylint
lint-pylint:
    uv run pylint src/

# Run all linters
lint: lint-ruff lint-pylint

# Auto-fix ruff violations and sort imports
fix:
    uv run ruff check --fix .
    uv run ruff format .

# Check formatting without writing changes
fmt-check:
    uv run ruff format --check .

# ── Database ────────────────────────────────────────────────────────────────

# Apply schema, constraints, triggers, RLS, and seed transition rules
db-migrate:
    uv run relay-migrate

# Drop and recreate the schema, then migrate (DESTRUCTIVE — dev only)
db-reset:
    uv run relay-migrate --reset

# Start a local Postgres cluster without Docker (Linux; uses .pgdata/)
db-local-start:
    ./scripts/dev_pg.sh start

# Stop the local no-Docker Postgres cluster
db-local-stop:
    ./scripts/dev_pg.sh stop

# ── Infrastructure (Docker) ─────────────────────────────────────────────────

# Start core infra: Postgres + Redis + Mailpit
infra-up:
    docker compose up -d postgres redis mailpit

# Start the full stack including the n8n workflow spine
stack-up:
    docker compose up -d

# Stop everything
stack-down:
    docker compose down

# ── Run ─────────────────────────────────────────────────────────────────────

# Run the FastAPI backend (dev, auto-reload)
api:
    uv run uvicorn relay.api.app:app --reload --port 8000

# Run the internal send worker once over pending jobs
worker:
    uv run relay-worker --once

# Run one retention purge pass (deletes leads past retention_until)
retention:
    uv run relay-retention

# Drain the SES/SNS event queue once (bounces/complaints -> suppression)
events:
    uv run relay-events

# Run reasoning evals against the configured backends (spends real quota)
evals which="both":
    uv run python scripts/run_evals.py {{which}}

# Walk a synthetic lead through the entire state machine and print the trace
demo:
    uv run python scripts/demo_journey.py

# Throughput benchmark  (usage: just bench 2 10 4 = tenants leads concurrency)
bench tenants="2" leads="10" concurrency="4":
    uv run python scripts/benchmark_throughput.py --tenants {{tenants}} --leads {{leads}} --concurrency {{concurrency}}

# Seed a synthetic campaign (Faker prospects incl. edge cases) and run it
seed n="20":
    uv run python scripts/seed_synthetic.py {{n}}

# ── Testing ─────────────────────────────────────────────────────────────────

# Run the full test suite (requires a reachable Postgres; see README)
test:
    uv run pytest tests/ -v

# Run tests with coverage
test-cov:
    uv run pytest tests/ -v --tb=short --cov=src/relay --cov-report=term-missing

# Run only the Phase 0 exit-gate tests
test-exit-gate:
    uv run pytest tests/ -v -m exit_gate
