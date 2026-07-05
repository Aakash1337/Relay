-- ─────────────────────────────────────────────────────────────────────────
-- Idempotent schema evolution for EXISTING databases. metadata.create_all
-- (which runs before these files) only creates missing tables — it never
-- alters existing ones — so every column/constraint added to a model after
-- a database first migrated needs a matching statement here. Fresh
-- databases get everything from the ORM metadata and these are no-ops.
-- ─────────────────────────────────────────────────────────────────────────

-- Phase 4: per-tenant quotas (NULL = fall back to global config).
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS daily_send_cap integer;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS monthly_spend_cap_units numeric(12, 2);
-- Phase 4: per-tenant sending identity (NULL = global RELAY_SES_FROM).
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS sender_from_address text;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS sender_identity_verified boolean
  NOT NULL DEFAULT false;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'ck_tenants_daily_send_cap'
  ) THEN
    ALTER TABLE tenants ADD CONSTRAINT ck_tenants_daily_send_cap
      CHECK (daily_send_cap IS NULL OR daily_send_cap >= 0);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'ck_tenants_spend_cap'
  ) THEN
    ALTER TABLE tenants ADD CONSTRAINT ck_tenants_spend_cap
      CHECK (monthly_spend_cap_units IS NULL OR monthly_spend_cap_units >= 0);
  END IF;
END
$$;

-- Phase 4: pipeline_runs.status gains 'killed_tenant_spend_cap'.
ALTER TABLE pipeline_runs DROP CONSTRAINT IF EXISTS ck_runs_status;
ALTER TABLE pipeline_runs ADD CONSTRAINT ck_runs_status
  CHECK (status IN ('running','completed','killed_iteration_cap',
                    'killed_budget','killed_tenant_spend_cap','failed'));

-- Multi-step sequences (§17, un-deferred by operator decision).
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS sequence_length integer
  NOT NULL DEFAULT 1;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS sequence_delay_hours integer
  NOT NULL DEFAULT 72;
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'ck_campaigns_sequence_length'
  ) THEN
    ALTER TABLE campaigns ADD CONSTRAINT ck_campaigns_sequence_length
      CHECK (sequence_length >= 1);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'ck_campaigns_sequence_delay'
  ) THEN
    ALTER TABLE campaigns ADD CONSTRAINT ck_campaigns_sequence_delay
      CHECK (sequence_delay_hours >= 0);
  END IF;
END
$$;
