# Enable EventBridge notifications on the existing landing bucket.
# NOTE: aws_s3_bucket_notification is authoritative for the bucket. If other
# notifications already exist on it, manage them here too or set
# manage_bucket_notification = false and enable EventBridge elsewhere.
resource "aws_s3_bucket_notification" "landing" {
  count       = var.manage_bucket_notification ? 1 : 0
  bucket      = var.landing_bucket
  eventbridge = true
}

# Route "Object Created" events for known client prefixes to the SQS buffer.
# The event pattern matches one prefix per client, so onboarding a client
# (adding it to var.clients) automatically starts routing its files.
resource "aws_cloudwatch_event_rule" "object_created" {
  name        = "${local.name}-transcription-object-created"
  description = "S3 Object Created in the landing bucket for known client prefixes"

  event_pattern = jsonencode({
    source      = ["aws.s3"]
    detail-type = ["Object Created"]
    detail = {
      bucket = { name = [var.landing_bucket] }
      object = {
        key = [for k in local.client_keys : { prefix = "${k}/" }]
      }
    }
  })

  tags = local.common_tags
}

resource "aws_cloudwatch_event_target" "to_sqs" {
  rule      = aws_cloudwatch_event_rule.object_created.name
  target_id = "transcriptions-queue"
  arn       = aws_sqs_queue.transcriptions.arn
}
