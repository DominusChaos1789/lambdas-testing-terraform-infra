# Dead-letter queue for poison messages (bad JSON, permanent API failures).
resource "aws_sqs_queue" "dlq" {
  name                      = "${local.name}-transcriptions-dlq"
  message_retention_seconds = 1209600 # 14 days
  tags                      = local.common_tags
}

# Main buffer between S3/EventBridge and the Lambda. Visibility timeout must be
# >= 6x the Lambda timeout so a message is not re-delivered while in flight.
resource "aws_sqs_queue" "transcriptions" {
  name                       = "${local.name}-transcriptions"
  visibility_timeout_seconds = var.lambda_timeout * 6
  message_retention_seconds  = 345600 # 4 days
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = var.max_receive_count
  })
  tags = local.common_tags
}

# Allow EventBridge to deliver S3 "Object Created" events into the queue.
data "aws_iam_policy_document" "queue_policy" {
  statement {
    sid    = "AllowEventBridgeSendMessage"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.transcriptions.arn]
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.object_created.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "transcriptions" {
  queue_url = aws_sqs_queue.transcriptions.id
  policy    = data.aws_iam_policy_document.queue_policy.json
}
