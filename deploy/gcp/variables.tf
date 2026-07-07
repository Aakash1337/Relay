variable "project_id" {
  description = "GCP project to deploy into."
  type        = string
}

variable "region" {
  description = "Region for the VM, Cloud SQL, and Artifact Registry. Default is us-east4: closest GCP region to AWS us-east-2, where RELAY's SES/SNS/SQS stack lives — keeps the cross-cloud hop short and the data residency US-based."
  type        = string
  default     = "us-east4"
}

variable "zone" {
  description = "Zone for the VM."
  type        = string
  default     = "us-east4-a"
}

variable "machine_type" {
  description = "VM machine type. e2-medium (4 GB) fits the full stack incl. n8n."
  type        = string
  default     = "e2-medium"
}

variable "db_tier" {
  description = "Cloud SQL tier. db-custom-1-3840 is the small always-valid choice; db-f1-micro is cheaper but shared-core and outside the SLA."
  type        = string
  default     = "db-custom-1-3840"
}

variable "image_tag" {
  description = "Tag of the relay image the VM boots."
  type        = string
  default     = "latest"
}

variable "enable_tunnel" {
  description = "Run the cloudflared ingress container. Requires the relay-tunnel-token secret to have a version."
  type        = bool
  default     = true
}

# Non-secret application configuration, written verbatim into the runtime
# env file on the VM. Anything from deploy/env.prod.example that is not a
# secret belongs here (RELAY_SES_FROM, AWS_REGION, RELAY_PILOT_RECIPIENTS,
# RELAY_COMPUTE_*, …). Secrets go in Secret Manager, never here.
variable "app_env" {
  description = "Non-secret env vars for the RELAY stack."
  type        = map(string)
  default     = {}
}
