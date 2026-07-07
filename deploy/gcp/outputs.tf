output "vm_external_ip" {
  description = "Static egress IP of the VM (ingress stays firewalled)."
  value       = google_compute_address.vm.address
}

output "sql_private_ip" {
  description = "Cloud SQL private IP (reachable only inside the VPC)."
  value       = google_sql_database_instance.relay.private_ip_address
}

output "image" {
  description = "Image URL the VM boots; push your build here."
  value       = local.image
}

output "ssh" {
  description = "SSH via IAP (no public SSH exposure)."
  value       = "gcloud compute ssh relay --zone ${var.zone} --tunnel-through-iap --project ${var.project_id}"
}

output "secrets_to_set" {
  description = "Operator-set secrets that need a version before first boot."
  value       = [for name, required in local.operator_secrets : name if required]
}
