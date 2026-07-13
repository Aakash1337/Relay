# Least-privilege credentials for the app: send through this identity
# with this configuration set, drain this one queue. Nothing else — no
# identity creation, no console, no wildcard SES.

data "aws_iam_policy_document" "app" {
  statement {
    sid    = "SendViaRelayIdentity"
    effect = "Allow"
    actions = [
      "ses:SendEmail",
      "ses:SendRawEmail",
    ]
    resources = [
      aws_sesv2_email_identity.domain.arn,
      aws_sesv2_configuration_set.relay.arn,
    ]
  }

  statement {
    sid    = "DrainEventsQueue"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.ses_events.arn]
  }
}

resource "aws_iam_user" "app" {
  count = var.create_iam_user ? 1 : 0
  name  = "${var.name_prefix}-app"
}

resource "aws_iam_user_policy" "app" {
  count  = var.create_iam_user ? 1 : 0
  name   = "${var.name_prefix}-app"
  user   = aws_iam_user.app[0].name
  policy = data.aws_iam_policy_document.app.json
}

resource "aws_iam_access_key" "app" {
  count = var.create_iam_user ? 1 : 0
  user  = aws_iam_user.app[0].name
}
