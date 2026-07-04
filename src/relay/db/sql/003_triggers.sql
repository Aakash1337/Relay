-- RELAY — trigger wiring. Idempotent: drop-if-exists then create.

-- Lead lifecycle -----------------------------------------------------------
DROP TRIGGER IF EXISTS trg_lead_insert_guard ON leads;
CREATE TRIGGER trg_lead_insert_guard
  BEFORE INSERT ON leads
  FOR EACH ROW EXECUTE FUNCTION fn_lead_insert_guard();

DROP TRIGGER IF EXISTS trg_enforce_lead_transition ON leads;
CREATE TRIGGER trg_enforce_lead_transition
  BEFORE UPDATE ON leads
  FOR EACH ROW EXECUTE FUNCTION fn_enforce_lead_transition();

DROP TRIGGER IF EXISTS trg_auto_suppress ON leads;
CREATE TRIGGER trg_auto_suppress
  AFTER UPDATE ON leads
  FOR EACH ROW
  WHEN (
    NEW.state IN ('unsubscribed', 'bounce_received')
    AND OLD.state IS DISTINCT FROM NEW.state
  )
  EXECUTE FUNCTION fn_auto_suppress();

-- Outreach drafts (human gate content) --------------------------------------
DROP TRIGGER IF EXISTS trg_draft_guard ON outreach_drafts;
CREATE TRIGGER trg_draft_guard
  BEFORE INSERT OR UPDATE ON outreach_drafts
  FOR EACH ROW EXECUTE FUNCTION fn_draft_guard();

-- Send jobs (the outbox) ----------------------------------------------------
DROP TRIGGER IF EXISTS trg_send_jobs_guard ON send_jobs;
CREATE TRIGGER trg_send_jobs_guard
  BEFORE INSERT OR UPDATE ON send_jobs
  FOR EACH ROW EXECUTE FUNCTION fn_send_jobs_guard();

-- Audit log is append-only ---------------------------------------------------
DROP TRIGGER IF EXISTS trg_audit_append_only ON audit_log;
CREATE TRIGGER trg_audit_append_only
  BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION fn_audit_append_only();

-- tenant_id immutable on every tenant-scoped table ---------------------------
DROP TRIGGER IF EXISTS trg_tenant_immutable ON lead_source_register;
CREATE TRIGGER trg_tenant_immutable
  BEFORE UPDATE ON lead_source_register
  FOR EACH ROW EXECUTE FUNCTION fn_tenant_immutable();

DROP TRIGGER IF EXISTS trg_tenant_immutable ON campaigns;
CREATE TRIGGER trg_tenant_immutable
  BEFORE UPDATE ON campaigns
  FOR EACH ROW EXECUTE FUNCTION fn_tenant_immutable();

DROP TRIGGER IF EXISTS trg_tenant_immutable ON leads;
CREATE TRIGGER trg_tenant_immutable
  BEFORE UPDATE ON leads
  FOR EACH ROW EXECUTE FUNCTION fn_tenant_immutable();

DROP TRIGGER IF EXISTS trg_tenant_immutable ON lead_transitions;
CREATE TRIGGER trg_tenant_immutable
  BEFORE UPDATE ON lead_transitions
  FOR EACH ROW EXECUTE FUNCTION fn_tenant_immutable();

DROP TRIGGER IF EXISTS trg_tenant_immutable ON suppression;
CREATE TRIGGER trg_tenant_immutable
  BEFORE UPDATE ON suppression
  FOR EACH ROW EXECUTE FUNCTION fn_tenant_immutable();

DROP TRIGGER IF EXISTS trg_tenant_immutable ON outreach_drafts;
CREATE TRIGGER trg_tenant_immutable
  BEFORE UPDATE ON outreach_drafts
  FOR EACH ROW EXECUTE FUNCTION fn_tenant_immutable();

DROP TRIGGER IF EXISTS trg_tenant_immutable ON send_jobs;
CREATE TRIGGER trg_tenant_immutable
  BEFORE UPDATE ON send_jobs
  FOR EACH ROW EXECUTE FUNCTION fn_tenant_immutable();

DROP TRIGGER IF EXISTS trg_tenant_immutable ON pipeline_runs;
CREATE TRIGGER trg_tenant_immutable
  BEFORE UPDATE ON pipeline_runs
  FOR EACH ROW EXECUTE FUNCTION fn_tenant_immutable();

-- Replies (inbound; simulated in Phase 1A) -----------------------------------
DROP TRIGGER IF EXISTS trg_reply_triage_guard ON replies;
CREATE TRIGGER trg_reply_triage_guard
  BEFORE UPDATE ON replies
  FOR EACH ROW EXECUTE FUNCTION fn_reply_triage_guard();

-- Draft reviews are append-only ----------------------------------------------
DROP TRIGGER IF EXISTS trg_draft_reviews_append_only ON draft_reviews;
CREATE TRIGGER trg_draft_reviews_append_only
  BEFORE UPDATE OR DELETE ON draft_reviews
  FOR EACH ROW EXECUTE FUNCTION fn_draft_reviews_append_only();
