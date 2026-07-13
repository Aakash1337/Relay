# The sending side: domain identity + DKIM + custom MAIL FROM + the
# configuration set every RELAY send tags (RELAY_SES_CONFIGURATION_SET).
# This replaces the by-hand console setup, identity by identity.

resource "aws_sesv2_email_identity" "domain" {
  email_identity         = var.sending_domain
  configuration_set_name = aws_sesv2_configuration_set.relay.configuration_set_name
}

# Custom MAIL FROM: bounces return through your own subdomain, which is
# what SPF alignment (and therefore DMARC) needs.
resource "aws_sesv2_email_identity_mail_from_attributes" "domain" {
  email_identity         = aws_sesv2_email_identity.domain.email_identity
  mail_from_domain       = "${var.mail_from_subdomain}.${var.sending_domain}"
  behavior_on_mx_failure = "REJECT_MESSAGE"
}

resource "aws_sesv2_configuration_set" "relay" {
  configuration_set_name = "${var.name_prefix}-events"

  delivery_options {
    tls_policy = "REQUIRE"
  }

  reputation_options {
    reputation_metrics_enabled = true
  }
}

# Every bounce/complaint/delivery flows: SES → SNS (signed) → SQS →
# relay-events → suppression. RELAY verifies the SNS envelope signature,
# so the subscription deliberately keeps the envelope (no raw delivery).
resource "aws_sesv2_configuration_set_event_destination" "sns" {
  configuration_set_name = aws_sesv2_configuration_set.relay.configuration_set_name
  event_destination_name = "${var.name_prefix}-sns"

  event_destination {
    enabled              = true
    matching_event_types = ["BOUNCE", "COMPLAINT", "DELIVERY", "REJECT"]

    sns_destination {
      topic_arn = aws_sns_topic.ses_events.arn
    }
  }
}
