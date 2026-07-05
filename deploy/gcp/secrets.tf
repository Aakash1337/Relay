# All secrets live in Secret Manager and are fetched into a tmpfs env file
# at VM boot — nothing sensitive in the image, in Terraform-rendered
# metadata, or on persistent disk.
#
# Two kinds:
#   - Terraform-managed: the two DB passwords (generated here). Caveat:
#     generated passwords live in the Terraform state — keep the state in a
#     private GCS bucket, not in git.
#   - Operator-set: everything else. Terraform creates the empty secret;
#     you add the value:  printf '%s' 'VALUE' | gcloud secrets versions add NAME --data-file=-

locals {
  # name → required at boot (startup fails loudly if a required secret has
  # no version; optional ones are skipped when absent).
  operator_secrets = {
    "relay-admin-token"           = true
    "relay-master-key"            = true
    "relay-email-hash-pepper"     = true
    "relay-aws-access-key-id"     = false
    "relay-aws-secret-access-key" = false
    "relay-google-api-key"        = false
    "relay-tunnel-token"          = false
    "relay-n8n-encryption-key"    = false
  }
}

resource "google_secret_manager_secret" "operator" {
  for_each  = local.operator_secrets
  secret_id = each.key

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "db_password" {
  secret_id = "relay-db-password"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "db_password" {
  secret      = google_secret_manager_secret.db_password.id
  secret_data = random_password.relay_db.result
}

resource "google_secret_manager_secret" "app_db_password" {
  secret_id = "relay-app-db-password"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "app_db_password" {
  secret      = google_secret_manager_secret.app_db_password.id
  secret_data = random_password.relay_app_db.result
}
