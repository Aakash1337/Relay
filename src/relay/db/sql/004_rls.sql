-- RELAY — row-level security and least-privilege grants.
--
-- The application role (relay_app) is not the table owner and has RLS
-- FORCEd: every query it runs is filtered to the tenant set in
-- app.tenant_id, and every write must match it. Cross-tenant reads and
-- writes are rejected by Postgres, not by application discipline.
--
-- Known boundary (documented, deliberate for Phase 0): the API derives
-- the tenant from API-key auth and pins it per transaction. A compromised
-- application process could set a different tenant GUC — per-tenant DB
-- credentials are the Phase 4 hardening step for that.

-- The SECURITY DEFINER functions (fn_is_suppressed's global scope,
-- fn_tenant_id_for_api_key, fn_tenants_with_queued_jobs) must read across
-- tenants. They run as the *owner* of the function — the migrating role. But
-- FORCE ROW LEVEL SECURITY binds even the table owner, so on managed Postgres
-- (RDS/Cloud SQL) where the owner is a plain, non-superuser role, those
-- functions would silently see nothing: global suppression would stop
-- working, API-key auth would 401 everything, and the worker would find no
-- work. (It only "works" on a superuser owner because superusers bypass RLS.)
-- The portable fix: a permissive policy scoped to the owner role, so the
-- definer functions get full visibility regardless of superuser status. This
-- is a harmless no-op when the owner is already a superuser.
DO $$
DECLARE
  owner_role text := current_user;
  t text;
BEGIN
  -- ── Tenants: a session sees only its own tenant row ──────────────────────
  ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
  ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
  DROP POLICY IF EXISTS tenant_self ON tenants;
  CREATE POLICY tenant_self ON tenants USING (id = fn_current_tenant());
  EXECUTE format('DROP POLICY IF EXISTS definer_bypass ON tenants');
  EXECUTE format(
    'CREATE POLICY definer_bypass ON tenants TO %I '
    'USING (true) WITH CHECK (true)',
    owner_role
  );

  -- ── Tenant-scoped tables ─────────────────────────────────────────────────
  FOREACH t IN ARRAY ARRAY[
    'lead_source_register', 'campaigns', 'leads', 'lead_transitions',
    'suppression', 'outreach_drafts', 'send_jobs', 'audit_log',
    'pipeline_runs', 'replies', 'draft_reviews', 'data_preflight'
  ]
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I '
      'USING (tenant_id = fn_current_tenant()) '
      'WITH CHECK (tenant_id = fn_current_tenant())',
      t
    );
    EXECUTE format('DROP POLICY IF EXISTS definer_bypass ON %I', t);
    EXECUTE format(
      'CREATE POLICY definer_bypass ON %I TO %I '
      'USING (true) WITH CHECK (true)',
      t, owner_role
    );
  END LOOP;
END
$$;

-- ── Least-privilege grants for the application role ─────────────────────────
GRANT USAGE ON SCHEMA public TO relay_app;

GRANT SELECT ON tenants TO relay_app;
GRANT SELECT ON lead_transition_rules TO relay_app;

GRANT SELECT, INSERT, UPDATE ON leads TO relay_app;
GRANT SELECT, INSERT ON campaigns TO relay_app;
GRANT SELECT, INSERT ON lead_source_register TO relay_app;
GRANT SELECT, INSERT ON lead_transitions TO relay_app;
GRANT SELECT, INSERT ON suppression TO relay_app;
GRANT SELECT, INSERT, UPDATE ON outreach_drafts TO relay_app;
GRANT SELECT, INSERT, UPDATE ON send_jobs TO relay_app;
GRANT SELECT, INSERT ON audit_log TO relay_app;
GRANT SELECT, INSERT, UPDATE ON pipeline_runs TO relay_app;
-- UPDATE only to record the triage outcome; the body itself is frozen by
-- fn_reply_triage_guard (triggers file).
GRANT SELECT, INSERT, UPDATE ON replies TO relay_app;
-- Reviews are append-only for the app role: no UPDATE grant, by design.
GRANT SELECT, INSERT ON draft_reviews TO relay_app;
-- Preflight approval is an admin-path act (owner role); the app role only
-- reads it — and the insert-guard trigger reads it under the app session.
GRANT SELECT ON data_preflight TO relay_app;

-- No DELETE grants anywhere. The only deletion capability the app role
-- has is fn_dsr_erase (below): the dedicated, audited, tenant-guarded
-- DSR/retention path — not a blanket grant.

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO relay_app;

-- SECURITY DEFINER functions: executable only by the app role.
REVOKE ALL ON FUNCTION fn_is_suppressed(uuid, text, text, uuid, text)
  FROM PUBLIC;
REVOKE ALL ON FUNCTION fn_tenant_id_for_api_key(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION fn_tenants_with_queued_jobs() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_is_suppressed(uuid, text, text, uuid, text)
  TO relay_app;
GRANT EXECUTE ON FUNCTION fn_tenant_id_for_api_key(text) TO relay_app;
GRANT EXECUTE ON FUNCTION fn_tenants_with_queued_jobs() TO relay_app;

-- DSR erasure: the app role's only deletion capability, tenant-guarded
-- inside the function itself.
REVOKE ALL ON FUNCTION fn_dsr_erase(uuid, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_dsr_erase(uuid, text) TO relay_app;

REVOKE ALL ON FUNCTION fn_tenants_with_expired_leads() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_tenants_with_expired_leads() TO relay_app;

REVOKE ALL ON FUNCTION fn_tenants_with_stale_work(double precision) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_tenants_with_stale_work(double precision)
  TO relay_app;

REVOKE ALL ON FUNCTION fn_tenants_for_recipient_hash(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION fn_tenants_for_recipient_hash(text) TO relay_app;
