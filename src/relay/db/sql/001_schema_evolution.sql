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

-- Human shortlist stage (gap-fill): campaigns opt in; two new lead states.
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS shortlist_required boolean
  NOT NULL DEFAULT false;

-- The state CHECKs enumerate every LeadState; refresh them for the two new
-- states (shortlist_pending, shortlist_skipped). Idempotent drop+add, same
-- pattern as ck_runs_status above. The list mirrors domain/states.py.
ALTER TABLE leads DROP CONSTRAINT IF EXISTS ck_leads_state;
ALTER TABLE leads ADD CONSTRAINT ck_leads_state
  CHECK (state IN ('created', 'source_checked', 'source_rejected', 'enrichment_pending', 'enriched', 'verification_pending', 'verification_failed', 'verified', 'scoring_pending', 'scored_rejected', 'scored_qualified', 'shortlist_pending', 'shortlist_skipped', 'personalization_pending', 'draft_ready', 'approval_pending', 'rejected_by_human', 'approved', 'send_eligibility_pending', 'send_blocked', 'send_queued', 'sent', 'bounce_received', 'reply_received', 'triage_pending', 'unsubscribed', 'not_interested', 'interested', 'booking_pending', 'booked', 'closed', 'error_retryable', 'error_terminal'));
ALTER TABLE leads DROP CONSTRAINT IF EXISTS ck_leads_error_return_state;
ALTER TABLE leads ADD CONSTRAINT ck_leads_error_return_state
  CHECK (error_return_state IS NULL OR error_return_state IN ('created', 'source_checked', 'source_rejected', 'enrichment_pending', 'enriched', 'verification_pending', 'verification_failed', 'verified', 'scoring_pending', 'scored_rejected', 'scored_qualified', 'shortlist_pending', 'shortlist_skipped', 'personalization_pending', 'draft_ready', 'approval_pending', 'rejected_by_human', 'approved', 'send_eligibility_pending', 'send_blocked', 'send_queued', 'sent', 'bounce_received', 'reply_received', 'triage_pending', 'unsubscribed', 'not_interested', 'interested', 'booking_pending', 'booked', 'closed', 'error_retryable', 'error_terminal'));
ALTER TABLE lead_transitions DROP CONSTRAINT IF EXISTS ck_transitions_from_state;
ALTER TABLE lead_transitions ADD CONSTRAINT ck_transitions_from_state
  CHECK (from_state IN ('created', 'source_checked', 'source_rejected', 'enrichment_pending', 'enriched', 'verification_pending', 'verification_failed', 'verified', 'scoring_pending', 'scored_rejected', 'scored_qualified', 'shortlist_pending', 'shortlist_skipped', 'personalization_pending', 'draft_ready', 'approval_pending', 'rejected_by_human', 'approved', 'send_eligibility_pending', 'send_blocked', 'send_queued', 'sent', 'bounce_received', 'reply_received', 'triage_pending', 'unsubscribed', 'not_interested', 'interested', 'booking_pending', 'booked', 'closed', 'error_retryable', 'error_terminal'));
ALTER TABLE lead_transitions DROP CONSTRAINT IF EXISTS ck_transitions_to_state;
ALTER TABLE lead_transitions ADD CONSTRAINT ck_transitions_to_state
  CHECK (to_state IN ('created', 'source_checked', 'source_rejected', 'enrichment_pending', 'enriched', 'verification_pending', 'verification_failed', 'verified', 'scoring_pending', 'scored_rejected', 'scored_qualified', 'shortlist_pending', 'shortlist_skipped', 'personalization_pending', 'draft_ready', 'approval_pending', 'rejected_by_human', 'approved', 'send_eligibility_pending', 'send_blocked', 'send_queued', 'sent', 'bounce_received', 'reply_received', 'triage_pending', 'unsubscribed', 'not_interested', 'interested', 'booking_pending', 'booked', 'closed', 'error_retryable', 'error_terminal'));
