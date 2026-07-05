resource "google_artifact_registry_repository" "relay" {
  repository_id = "relay"
  location      = var.region
  format        = "DOCKER"
  description   = "RELAY application images"

  depends_on = [google_project_service.apis]
}

locals {
  registry_host = "${var.region}-docker.pkg.dev"
  image         = "${local.registry_host}/${var.project_id}/${google_artifact_registry_repository.relay.repository_id}/relay:${var.image_tag}"
}
