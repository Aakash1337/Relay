# Cloud SQL for PostgreSQL 16, private IP only, SSL enforced.
#
# The role gotcha, handled: RELAY uses two roles — `relay` (owns the schema,
# runs migrations) and `relay_app` (RLS-constrained runtime role that
# migrate.py creates). Cloud SQL has no true superuser, but users created
# through the API (like `relay` below) are members of `cloudsqlsuperuser`,
# which carries CREATEROLE — exactly what migrate.py's
# `CREATE ROLE relay_app WITH LOGIN PASSWORD …` needs. Nothing in the
# migration path requires real superuser (verified: no CREATE EXTENSION, no
# ALTER SYSTEM; FORCE RLS only needs table ownership, and `relay` owns the
# tables). So the stock migrator runs unmodified.
#
# `relay_app` is deliberately NOT a google_sql_user: migrate.py creates and
# owns it, using the password from the relay-app-db-password secret.

resource "google_sql_database_instance" "relay" {
  name             = "relay"
  database_version = "POSTGRES_16"
  region           = var.region

  depends_on = [google_service_networking_connection.sql]

  settings {
    tier              = var.db_tier
    availability_type = "ZONAL"
    disk_size         = 10
    disk_autoresize   = true

    ip_configuration {
      ipv4_enabled    = false
      private_network = google_compute_network.relay.id
      ssl_mode        = "ENCRYPTED_ONLY"
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }
  }

  deletion_protection = true
}

resource "google_sql_database" "relay" {
  name     = "relay"
  instance = google_sql_database_instance.relay.name
}

resource "random_password" "relay_db" {
  length = 32
  # URL-safe: these passwords are embedded in DSNs.
  override_special = "-_"
}

resource "random_password" "relay_app_db" {
  length           = 32
  override_special = "-_"
}

resource "google_sql_user" "relay" {
  name     = "relay"
  instance = google_sql_database_instance.relay.name
  password = random_password.relay_db.result
}
