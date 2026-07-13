# The return path: SNS topic → SQS queue that relay-events polls
# (pull-based — nothing here needs public ingress anywhere).

resource "aws_sns_topic" "ses_events" {
  name = "${var.name_prefix}-ses-events"
}

resource "aws_sqs_queue" "ses_events" {
  name                       = "${var.name_prefix}-ses-events"
  message_retention_seconds  = 345600 # 4 days of poller downtime tolerance
  visibility_timeout_seconds = 60
  # Long polling at the queue level; harmless if the client overrides it.
  receive_wait_time_seconds = 20
}

resource "aws_sns_topic_subscription" "ses_to_sqs" {
  topic_arn = aws_sns_topic.ses_events.arn
  protocol  = "sqs"
  endpoint  = aws_sqs_queue.ses_events.arn
  # raw_message_delivery stays false: RELAY's ingest verifies the SNS
  # envelope signature, so it needs the envelope intact.
}

data "aws_iam_policy_document" "queue_policy" {
  statement {
    sid     = "AllowSnsDelivery"
    effect  = "Allow"
    actions = ["sqs:SendMessage"]

    principals {
      type        = "Service"
      identifiers = ["sns.amazonaws.com"]
    }

    resources = [aws_sqs_queue.ses_events.arn]

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_sns_topic.ses_events.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "ses_events" {
  queue_url = aws_sqs_queue.ses_events.id
  policy    = data.aws_iam_policy_document.queue_policy.json
}
