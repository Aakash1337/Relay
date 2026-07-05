# One VM, docker compose up. Always-on by requirement: the send/event/
# retention workers and the n8n spine run whether or not an HTTP request is
# happening, which rules out scale-to-zero platforms (Cloud Run et al.).
# GKE is the future option if this outgrows one box; not built here.

resource "google_service_account" "vm" {
  account_id   = "relay-vm"
  display_name = "RELAY VM"
}

# Least privilege: read access to exactly the RELAY secrets, pull access to
# the registry, and log/metric writing. No project-wide roles.
resource "google_secret_manager_secret_iam_member" "operator" {
  for_each  = google_secret_manager_secret.operator
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.vm.email}"
}

resource "google_secret_manager_secret_iam_member" "db_password" {
  secret_id = google_secret_manager_secret.db_password.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.vm.email}"
}

resource "google_secret_manager_secret_iam_member" "app_db_password" {
  secret_id = google_secret_manager_secret.app_db_password.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.vm.email}"
}

resource "google_artifact_registry_repository_iam_member" "vm_pull" {
  repository = google_artifact_registry_repository.relay.name
  location   = var.region
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.vm.email}"
}

resource "google_project_iam_member" "vm_logging" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.vm.email}"
}

resource "google_project_iam_member" "vm_monitoring" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.vm.email}"
}

resource "google_compute_instance" "relay" {
  name         = "relay"
  zone         = var.zone
  machine_type = var.machine_type
  tags         = ["relay-vm"]

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
      size  = 30
      type  = "pd-balanced"
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.relay.id

    access_config {
      nat_ip = google_compute_address.vm.address
    }
  }

  service_account {
    email  = google_service_account.vm.email
    scopes = ["cloud-platform"]
  }

  # Runs on every boot: fetch secrets → assemble tmpfs env file → pull the
  # image → compose up. Re-running is idempotent (and migrate is idempotent).
  metadata_startup_script = templatefile("${path.module}/startup.sh.tpl", {
    project_id    = var.project_id
    registry_host = local.registry_host
    image         = local.image
    db_host       = google_sql_database_instance.relay.private_ip_address
    enable_tunnel = var.enable_tunnel
    app_env       = var.app_env
    compose_yaml  = file("${path.module}/../docker-compose.prod.yml")
  })

  allow_stopping_for_update = true

  depends_on = [google_sql_user.relay]
}
