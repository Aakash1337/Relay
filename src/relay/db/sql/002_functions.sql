-- RELAY — database functions.
-- These implement the structural guarantees: transition legality, the
-- suppression invariant, dry-run send prevention, append-only audit.
-- They run for EVERY write, whatever code (or bug) issued it.
-- All statements are idempotent (CREATE OR REPLACE).

-- ─────────────────────────────────────────────────────────────────────────
-- Tenant context. The application role sets app.tenant_id per transaction;
-- RLS policies (004_rls.sql) compare against this.
-- ─────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_current_tenant() RETURNS uuid
LANGUAGE sql STABLE
AS $$
  SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid
$$;

-- ─────────────────────────────────────────────────────────────────────────
-- Suppression check (§10). SECURITY DEFINER so the 'global' scope can see
-- rows across tenants even under RLS: over-suppression is the safe
-- direction. Scope precedence is irrelevant — any live match suppresses.
--
-- Cross-tenant probe guard: SECURITY DEFINER means this bypasses RLS, so a
-- tenant-scoped caller must not be able to probe another tenant's
-- suppression list. When a tenant context is set (fn_current_tenant() is
-- not null), p_tenant must equal it. Untenanted callers (the worker's
-- trigger path, which sets no GUC) are unaffected, and the 'global' branch
-- does not depend on p_tenant so cross-tenant suppression still applies.
-- ─────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_is_suppressed(
  p_tenant uuid,
  p_email_hash text,
  p_domain text,
  p_campaign uuid,
  p_mailbox text
) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  v_ctx uuid := fn_current_tenant();
BEGIN
  IF v_ctx IS NOT NULL AND p_tenant IS DISTINCT FROM v_ctx THEN
    RAISE EXCEPTION 'cross-tenant suppression probe rejected'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN EXISTS (
    SELECT 1
    FROM suppression s
    WHERE (s.expires_at IS NULL OR s.expires_at > now())
      AND s.applies_to_sales
      AND (
        (s.scope = 'global' AND s.email_hash = p_email_hash)
        OR (
          s.tenant_id = p_tenant
          AND (
            (s.scope = 'tenant' AND s.email_hash = p_email_hash)
            OR (s.scope = 'domain' AND s.domain = p_domain)
            OR (
              s.scope = 'campaign'
              AND s.campaign_id = p_campaign
              AND s.email_hash = p_email_hash
            )
            OR (
              s.scope = 'mailbox'
              AND s.mailbox_id = p_mailbox
              AND s.email_hash = p_email_hash
            )
          )
        )
      )
  );
END
$$;

-- ─────────────────────────────────────────────────────────────────────────
-- API-key → tenant lookup. SECURITY DEFINER because it runs before any
-- tenant context exists. Only the hash ever reaches the database.
-- ─────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_tenant_id_for_api_key(p_hash text)
RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
  SELECT id FROM tenants WHERE api_key_hash = p_hash
$$;

-- ─────────────────────────────────────────────────────────────────────────
-- Which tenants have queued send jobs. The internal worker iterates these
-- and processes each tenant's jobs under that tenant's own RLS context —
-- the worker never operates outside a tenant scope.
-- ─────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_tenants_with_queued_jobs()
RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
  SELECT DISTINCT tenant_id FROM send_jobs WHERE status = 'queued'
$$;

-- ─────────────────────────────────────────────────────────────────────────
-- tenant_id is immutable everywhere. "No cross-tenant transition is ever
-- possible" (§4) — a row can never migrate between tenants.
-- ─────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_tenant_immutable() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id THEN
    RAISE EXCEPTION 'tenant_id is immutable (cross-tenant move rejected)'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────
-- Lead INSERT guard: leads are born in 'created' (no state-machine bypass
-- at insert), and only from a register entry whose terms allow the use
-- (§7 hard rule, checked live against the register).
-- ─────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_lead_insert_guard() RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  v_terms text;
BEGIN
  IF NEW.state <> 'created' THEN
    RAISE EXCEPTION
      'leads must be inserted in state ''created'' (got %)', NEW.state
      USING ERRCODE = 'check_violation';
  END IF;

  SELECT terms_allow_use INTO v_terms
  FROM lead_source_register
  WHERE tenant_id = NEW.tenant_id AND id = NEW.source_id;

  IF v_terms IS NULL THEN
    RAISE EXCEPTION 'lead source % not found in register', NEW.source_id
      USING ERRCODE = 'check_violation';
  END IF;

  IF v_terms <> 'yes' THEN
    RAISE EXCEPTION
      'lead source % terms do not allow use (register says %)',
      NEW.source_id, v_terms
      USING ERRCODE = 'check_violation';
  END IF;

  RETURN NEW;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────
-- THE lead transition guard (§4 invariants). Runs BEFORE every UPDATE on
-- leads. The planner advises; this trigger decides.
-- ─────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_enforce_lead_transition() RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  v_campaign campaigns%ROWTYPE;
BEGIN
  -- Immutable columns: identity, provenance, and the dry-run flag.
  IF NEW.dry_run IS DISTINCT FROM OLD.dry_run THEN
    RAISE EXCEPTION 'dry_run is immutable on leads'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.email IS DISTINCT FROM OLD.email
     OR NEW.email_hash IS DISTINCT FROM OLD.email_hash
     OR NEW.email_domain IS DISTINCT FROM OLD.email_domain THEN
    RAISE EXCEPTION 'lead email identity is immutable'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.source_id IS DISTINCT FROM OLD.source_id
     OR NEW.source_terms_status IS DISTINCT FROM OLD.source_terms_status
     OR NEW.lawful_basis IS DISTINCT FROM OLD.lawful_basis
     OR NEW.region_assumption IS DISTINCT FROM OLD.region_assumption THEN
    RAISE EXCEPTION 'lead provenance fields are immutable'
      USING ERRCODE = 'check_violation';
  END IF;

  -- The retry cap is only a guardrail if its inputs cannot be edited by the
  -- code it polices. max_retries is immutable; retry_count is managed solely
  -- by this trigger (the resume branch below), never by a caller.
  IF NEW.max_retries IS DISTINCT FROM OLD.max_retries THEN
    RAISE EXCEPTION 'max_retries is immutable'
      USING ERRCODE = 'check_violation';
  END IF;

  -- retry_count may only change on the state-changing resume branch below;
  -- forbid it when the state is unchanged (blocks a no-op reset).
  IF NEW.state IS NOT DISTINCT FROM OLD.state
     AND NEW.retry_count IS DISTINCT FROM OLD.retry_count THEN
    RAISE EXCEPTION 'retry_count is managed by the state machine'
      USING ERRCODE = 'check_violation';
  END IF;

  IF NEW.state IS DISTINCT FROM OLD.state THEN
    -- 1. Transition must exist in the seeded rule set.
    IF NOT EXISTS (
      SELECT 1 FROM lead_transition_rules r
      WHERE r.from_state = OLD.state AND r.to_state = NEW.state
    ) THEN
      RAISE EXCEPTION 'illegal lead state transition: % -> %',
        OLD.state, NEW.state
        USING ERRCODE = 'check_violation';
    END IF;

    -- 2. Retry cap: a retryable error resumes ONLY to the state it errored
    --    from (or fails terminally). The generic rule set allows
    --    error_retryable -> any active state; this narrows it structurally
    --    so a two-hop (X -> error_retryable -> Y) cannot skip the pipeline
    --    sequence. The counter is incremented here and capped.
    IF OLD.state = 'error_retryable' THEN
      IF NEW.state <> 'error_terminal' THEN
        IF OLD.error_return_state IS NULL
           OR NEW.state <> OLD.error_return_state THEN
          RAISE EXCEPTION
            'error_retryable may only resume to its error_return_state '
            '(% ) or error_terminal; got %',
            OLD.error_return_state, NEW.state
            USING ERRCODE = 'check_violation';
        END IF;
        NEW.retry_count := OLD.retry_count + 1;
        IF NEW.retry_count > OLD.max_retries THEN
          RAISE EXCEPTION 'retry cap exceeded (% of % used)',
            NEW.retry_count, OLD.max_retries
            USING ERRCODE = 'check_violation';
        END IF;
      END IF;
    ELSIF NEW.retry_count IS DISTINCT FROM OLD.retry_count THEN
      RAISE EXCEPTION 'retry_count is managed by the state machine'
        USING ERRCODE = 'check_violation';
    END IF;

    -- 3. Send-eligibility invariants: 'sent' requires approved AND
    --    send-eligible; both send states re-check structurally.
    IF NEW.state IN ('send_queued', 'sent') THEN
      SELECT * INTO v_campaign
      FROM campaigns c
      WHERE c.tenant_id = NEW.tenant_id AND c.id = NEW.campaign_id;

      IF NOT NEW.email_verified THEN
        RAISE EXCEPTION 'cannot enter %: email not verified', NEW.state
          USING ERRCODE = 'check_violation';
      END IF;
      IF NEW.approved_message_version IS NULL OR NOT EXISTS (
        SELECT 1 FROM outreach_drafts d
        WHERE d.tenant_id = NEW.tenant_id
          AND d.lead_id = NEW.id
          AND d.status = 'approved'
          AND d.version = NEW.approved_message_version
      ) THEN
        RAISE EXCEPTION
          'cannot enter %: no human-approved draft for message version %',
          NEW.state, NEW.approved_message_version
          USING ERRCODE = 'check_violation';
      END IF;
      IF fn_is_suppressed(
        NEW.tenant_id, NEW.email_hash, NEW.email_domain,
        NEW.campaign_id, v_campaign.mailbox_id
      ) THEN
        RAISE EXCEPTION 'cannot enter %: recipient is suppressed', NEW.state
          USING ERRCODE = 'check_violation';
      END IF;
    END IF;

    -- 4. 'sent' additionally requires an executing/executed send job, and
    --    a dry-run lead may never be 'sent' via a real job.
    IF NEW.state = 'sent' THEN
      IF NOT EXISTS (
        SELECT 1 FROM send_jobs sj
        WHERE sj.tenant_id = NEW.tenant_id
          AND sj.lead_id = NEW.id
          AND sj.status IN ('sending', 'sent')
      ) THEN
        RAISE EXCEPTION 'cannot enter sent: no send job in sending/sent'
          USING ERRCODE = 'check_violation';
      END IF;
      IF (NEW.dry_run OR v_campaign.dry_run) AND EXISTS (
        SELECT 1 FROM send_jobs sj
        WHERE sj.tenant_id = NEW.tenant_id
          AND sj.lead_id = NEW.id
          AND sj.mode = 'real'
      ) THEN
        RAISE EXCEPTION
          'structural violation: dry-run lead has a real send job'
          USING ERRCODE = 'check_violation';
      END IF;
    END IF;

    -- 5. Dry-run leads cannot receive replies outside explicit seed/test
    --    mode (§4).
    IF NEW.state = 'reply_received' THEN
      SELECT * INTO v_campaign
      FROM campaigns c
      WHERE c.tenant_id = NEW.tenant_id AND c.id = NEW.campaign_id;
      IF (NEW.dry_run OR v_campaign.dry_run)
         AND NOT v_campaign.simulated_replies_enabled THEN
        RAISE EXCEPTION
          'dry-run leads cannot receive replies outside seed/test mode'
          USING ERRCODE = 'check_violation';
      END IF;
    END IF;

    -- 6. 'booked' requires a linked reply and calendar reference (§4).
    IF NEW.state = 'booked'
       AND (NEW.replied_at IS NULL OR NEW.booking_ref IS NULL) THEN
      RAISE EXCEPTION
        'cannot enter booked: requires linked reply and booking reference'
        USING ERRCODE = 'check_violation';
    END IF;

    -- 7. Unsubscribe timestamp lands with the state.
    IF NEW.state = 'unsubscribed' AND NEW.unsubscribed_at IS NULL THEN
      NEW.unsubscribed_at := now();
    END IF;
  END IF;

  NEW.updated_at := now();
  RETURN NEW;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────
-- Send-job guard (§10): the outbox is defended on INSERT and again when a
-- worker claims the job. Approval alone does not send; suppression,
-- dry-run, and approval are re-checked at execution time.
-- ─────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_send_jobs_guard() RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  v_lead leads%ROWTYPE;
  v_campaign campaigns%ROWTYPE;
BEGIN
  SELECT * INTO v_lead
  FROM leads l
  WHERE l.tenant_id = NEW.tenant_id AND l.id = NEW.lead_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'send job references unknown lead'
      USING ERRCODE = 'check_violation';
  END IF;

  -- The recipient identity on the job MUST be the lead's own address.
  -- Otherwise the suppression re-check below (which uses the job's
  -- recipient_email_hash) could be defeated by a job whose hash points at
  -- an unsuppressed address while the lead itself is suppressed.
  IF NEW.recipient_email_hash IS DISTINCT FROM v_lead.email_hash
     OR NEW.recipient_domain IS DISTINCT FROM v_lead.email_domain THEN
    RAISE EXCEPTION
      'send job recipient must match its lead''s verified address'
      USING ERRCODE = 'check_violation';
  END IF;

  SELECT * INTO v_campaign
  FROM campaigns c
  WHERE c.tenant_id = NEW.tenant_id AND c.id = NEW.campaign_id;

  -- A real send job can never exist for a dry-run lead or campaign.
  IF NEW.mode = 'real' AND (v_lead.dry_run OR v_campaign.dry_run) THEN
    RAISE EXCEPTION
      'structural violation: real send job for dry-run lead/campaign'
      USING ERRCODE = 'check_violation';
  END IF;

  IF TG_OP = 'UPDATE' THEN
    -- Identity and idempotency fields are frozen at insert.
    IF NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
       OR NEW.lead_id IS DISTINCT FROM OLD.lead_id
       OR NEW.campaign_id IS DISTINCT FROM OLD.campaign_id
       OR NEW.draft_id IS DISTINCT FROM OLD.draft_id
       OR NEW.sequence_step IS DISTINCT FROM OLD.sequence_step
       OR NEW.message_version IS DISTINCT FROM OLD.message_version
       OR NEW.recipient_email_hash IS DISTINCT FROM OLD.recipient_email_hash
       OR NEW.mode IS DISTINCT FROM OLD.mode THEN
      RAISE EXCEPTION 'send job identity fields are immutable'
        USING ERRCODE = 'check_violation';
    END IF;

    -- Job status machine: queued → sending|blocked|failed,
    -- sending → sent|failed. Everything else is frozen.
    IF NEW.status IS DISTINCT FROM OLD.status THEN
      IF NOT (
        (OLD.status = 'queued'
         AND NEW.status IN ('sending', 'blocked', 'failed'))
        OR (OLD.status = 'sending' AND NEW.status IN ('sent', 'failed'))
      ) THEN
        RAISE EXCEPTION 'illegal send job status change: % -> %',
          OLD.status, NEW.status
          USING ERRCODE = 'check_violation';
      END IF;
    END IF;
  END IF;

  IF TG_OP = 'INSERT' THEN
    IF NEW.status <> 'queued' THEN
      RAISE EXCEPTION
        'send jobs must be inserted as queued (got %)', NEW.status
        USING ERRCODE = 'check_violation';
    END IF;
    IF v_lead.state <> 'send_queued' THEN
      RAISE EXCEPTION
        'send job requires its lead in send_queued (lead is %)',
        v_lead.state
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;

  -- Suppression is re-checked on INSERT and again at claim time
  -- (queued → sending): a suppressed recipient can never be sent to,
  -- regardless of what any code path believes.
  IF TG_OP = 'INSERT'
     OR (NEW.status = 'sending' AND OLD.status IS DISTINCT FROM 'sending')
  THEN
    IF fn_is_suppressed(
      NEW.tenant_id, NEW.recipient_email_hash, NEW.recipient_domain,
      NEW.campaign_id, NEW.mailbox_id
    ) THEN
      RAISE EXCEPTION 'structural violation: recipient is suppressed'
        USING ERRCODE = 'check_violation';
    END IF;
    IF NOT EXISTS (
      SELECT 1 FROM outreach_drafts d
      WHERE d.tenant_id = NEW.tenant_id
        AND d.lead_id = NEW.lead_id
        AND d.status = 'approved'
        AND d.version = NEW.message_version
    ) THEN
      RAISE EXCEPTION
        'no human-approved draft for message version %', NEW.message_version
        USING ERRCODE = 'check_violation';
    END IF;
    -- Claiming a job whose lead has moved on (error, block) is illegal.
    IF TG_OP = 'UPDATE' AND v_lead.state <> 'send_queued' THEN
      RAISE EXCEPTION
        'cannot claim send job: lead no longer in send_queued (is %)',
        v_lead.state
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;

  RETURN NEW;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────
-- Draft tamper-evidence (§10: "human approval exists for that exact message
-- version"). Once a draft is approved, its content is frozen — the send
-- worker reads draft.body at execution time, so mutable approved content
-- would let a send carry text no human ever saw. Also enforces the draft
-- status machine and requires approver metadata on approval.
-- ─────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_draft_guard() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    -- Version is the join key to lead.approved_message_version; never moves.
    IF NEW.version IS DISTINCT FROM OLD.version THEN
      RAISE EXCEPTION 'draft version is immutable'
        USING ERRCODE = 'check_violation';
    END IF;

    -- Content is frozen the moment a draft is approved, and can never
    -- change while it stays approved.
    IF OLD.status = 'approved' AND (
         NEW.subject IS DISTINCT FROM OLD.subject
      OR NEW.body IS DISTINCT FROM OLD.body
      OR NEW.status IS DISTINCT FROM OLD.status
    ) THEN
      RAISE EXCEPTION 'an approved draft is immutable (content frozen)'
        USING ERRCODE = 'check_violation';
    END IF;

    -- Legal status moves only: draft -> pending_approval ->
    -- approved | rejected. Nothing re-opens a decided draft.
    IF NEW.status IS DISTINCT FROM OLD.status
       AND NOT (
         (OLD.status = 'draft' AND NEW.status = 'pending_approval')
         OR (OLD.status = 'pending_approval'
             AND NEW.status IN ('approved', 'rejected'))
       ) THEN
      RAISE EXCEPTION 'illegal draft status change: % -> %',
        OLD.status, NEW.status
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;

  -- Approval must be attributable: status='approved' requires approver
  -- metadata, so a draft cannot be flipped approved with a NULL approver.
  IF NEW.status = 'approved'
     AND (NEW.approved_by IS NULL OR NEW.approved_at IS NULL) THEN
    RAISE EXCEPTION 'an approved draft requires approved_by and approved_at'
      USING ERRCODE = 'check_violation';
  END IF;

  RETURN NEW;
END
$$;

-- ─────────────────────────────────────────────────────────────────────────
-- Audit log is append-only.
-- ─────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_audit_append_only() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only'
    USING ERRCODE = 'check_violation';
END
$$;

-- ─────────────────────────────────────────────────────────────────────────
-- Unsubscribes and hard bounces auto-suppress (§6/§10): entering the state
-- creates the suppression entry in the same transaction — not a separate
-- code path that could be skipped.
-- ─────────────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_auto_suppress() RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  INSERT INTO suppression (
    tenant_id, scope, email_hash, domain, reason, source,
    created_by, applies_to_marketing, applies_to_sales
  )
  VALUES (
    NEW.tenant_id,
    'tenant',
    NEW.email_hash,
    NEW.email_domain,
    CASE WHEN NEW.state = 'unsubscribed'
         THEN 'unsubscribe' ELSE 'hard_bounce' END,
    'system',
    'trigger:fn_auto_suppress',
    true,
    true
  );
  RETURN NEW;
END
$$;
