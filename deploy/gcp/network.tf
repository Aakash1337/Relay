# Dedicated VPC. Ingress is deny-by-default (implied); the ONLY allow rule
# is SSH over IAP. The API is reached through the outbound-only cloudflared
# tunnel, so no inbound port is ever opened for it.

resource "google_compute_network" "relay" {
  name                    = "relay"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "relay" {
  name          = "relay"
  network       = google_compute_network.relay.id
  region        = var.region
  ip_cidr_range = "10.10.0.0/24"
}

resource "google_compute_firewall" "allow_iap_ssh" {
  name    = "relay-allow-iap-ssh"
  network = google_compute_network.relay.id

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  # Google's IAP TCP-forwarding range — `gcloud compute ssh --tunnel-through-iap`.
  source_ranges = ["35.235.240.0/20"]
  target_tags   = ["relay-vm"]
}

# Egress is allowed by default, which the stack depends on: SES + SQS on
# AWS (cross-cloud, deliberate), the Gemini API, Cloudflare's edge, and
# Artifact Registry. No egress rule needed; do not add a deny-all.

# Private services access so Cloud SQL gets a private IP inside this VPC.
resource "google_compute_global_address" "sql_range" {
  name          = "relay-sql-range"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = google_compute_network.relay.id
}

resource "google_service_networking_connection" "sql" {
  network                 = google_compute_network.relay.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.sql_range.name]
}

# Static external IP: a stable egress identity (useful if you ever allowlist
# the VM at AWS or a partner) and a stable SSH target. Ingress to it stays
# closed by the firewall either way.
resource "google_compute_address" "vm" {
  name   = "relay-vm"
  region = var.region
}
