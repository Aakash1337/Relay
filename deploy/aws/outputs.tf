output "dns_records" {
  description = "Paste these into the sending domain's DNS. Identity verifies once the DKIM CNAMEs resolve."
  value = concat(
    [
      for token in aws_sesv2_email_identity.domain.dkim_signing_attributes[0].tokens :
      {
        type  = "CNAME"
        name  = "${token}._domainkey.${var.sending_domain}"
        value = "${token}.dkim.amazonses.com"
      }
    ],
    [
      {
        type  = "MX"
        name  = "${var.mail_from_subdomain}.${var.sending_domain}"
        value = "10 feedback-smtp.${var.aws_region}.amazonses.com"
      },
      {
        type  = "TXT"
        name  = "${var.mail_from_subdomain}.${var.sending_domain}"
        value = "v=spf1 include:amazonses.com ~all"
      },
      {
        type  = "TXT"
        name  = "_dmarc.${var.sending_domain}"
        value = "v=DMARC1; p=none; rua=mailto:dmarc-reports@${var.sending_domain}"
      },
    ]
  )
}

output "env_values" {
  description = "What goes where in RELAY's environment."
  value = {
    AWS_REGION                  = var.aws_region
    RELAY_SES_CONFIGURATION_SET = aws_sesv2_configuration_set.relay.configuration_set_name
    RELAY_SQS_QUEUE_URL         = aws_sqs_queue.ses_events.url
  }
}

output "aws_access_key_id" {
  description = "Access key for the relay app IAM user (empty if create_iam_user=false)."
  value       = var.create_iam_user ? aws_iam_access_key.app[0].id : ""
}

output "aws_secret_access_key" {
  description = "Secret for the relay app IAM user. Sensitive: terraform output -raw aws_secret_access_key"
  value       = var.create_iam_user ? aws_iam_access_key.app[0].secret : ""
  sensitive   = true
}

output "identity_verification" {
  description = "Check with: aws sesv2 get-email-identity --email-identity <domain>"
  value       = "aws sesv2 get-email-identity --email-identity ${var.sending_domain} --region ${var.aws_region}"
}
