variable "aws_region" {
  description = "Region for SES/SNS/SQS. Keep everything in one region; us-east-2 is where the pilot lives."
  type        = string
  default     = "us-east-2"
}

variable "sending_domain" {
  description = "The domain mail is sent from (e.g. outreach.example.com). Verified as a SES domain identity; DKIM/MAIL FROM records are output for your DNS."
  type        = string
}

variable "mail_from_subdomain" {
  description = "Subdomain for the custom MAIL FROM (bounce return path), created under sending_domain."
  type        = string
  default     = "mail"
}

variable "name_prefix" {
  description = "Prefix for every named resource, so multiple environments can share an account."
  type        = string
  default     = "relay"
}

variable "create_iam_user" {
  description = "Create the least-privilege IAM user + access key for the app (ses:Send* on this identity, receive on the events queue). The secret lands in the Terraform state — keep the state private, or set false and manage credentials yourself."
  type        = bool
  default     = true
}
