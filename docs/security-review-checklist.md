# Security review checklist

The working checklist for the human security review
([prototype-status.md](prototype-status.md), go-to-production item 4).
The automated audits and adversarial tests in this repo are *input* to
this review, not a substitute for it. Extend this list as the review
proceeds; strike nothing without a note saying why.

1. **Render the actual per-service, per-environment config and confirm
   each service's DB role/DSN and secret assignment — do not infer it
   from the code alone.** Run `docker compose -f <file> config` (and
   the Terraform-rendered startup script / env file for cloud deploys)
   and check, service by service, which DSN and which secrets each one
   actually receives.
   *Why this is its own step:* in the cloud-infra work the application
   code was correct — workers only ever open `relay_app` sessions — but
   the compose file handed the admin DSN to every service via a shared
   env anchor. Only inspecting the rendered config caught it.
2. **Verify the database enforcement layer against a live instance**,
   not just the test suite: forced RLS on every tenant table, the
   transition trigger, the dry-run and suppression guards, and that
   `relay_app` really lacks the grants it's supposed to lack (try the
   forbidden DELETEs/UPDATEs by hand).
3. **Secrets inventory**: where each secret lives at rest (Secret
   Manager vs `.env` vs state files), who/what can read it (IAM
   bindings, Terraform state access), and that rotation paths
   (`RELAY_MASTER_KEY_PREVIOUS`, tenant key rotation) actually work.
4. **Network exposure**: enumerate every listening port and public
   route on a deployed instance; confirm only the tunnel-fronted API
   and `/unsubscribe` are reachable, and that admin/tenant auth gates
   every mutating endpoint.
5. **Prompt-injection surface**: confirm all externally sourced text
   reaches prompts only through the escaping/tagging seam, and re-run
   the adversarial evals against the *production-configured* models,
   not the offline stub.
6. **Audit trail + PII**: sample real audit rows and logs from a
   deployed instance for PII leaks; verify erasure leaves only the
   hashed do-not-contact marker, including in backups per the
   backup/restore test's procedure.
