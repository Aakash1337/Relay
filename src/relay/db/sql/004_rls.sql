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

-- ── Tenants: a session sees only its own tenant row ────────────────────────
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_self ON tenants;
CREATE POLICY tenant_self ON tenants
  USING (id = fn_current_tenant());

-- ── Tenant-scoped tables ────────────────────────────────────────────────────
DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'lead_source_register', 'campaigns', 'leads', 'lead_transitions',
    'suppression', 'outreach_drafts', 'send_jobs', 'audit_log',
    'pipeline_runs'
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

-- No DELETE grants anywhere: Phase 0 has no deletion path. The DSR /
-- right-to-be-forgotten workflow (Phase 1B) will add a dedicated,
-- audited path — not a blanket grant.

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
