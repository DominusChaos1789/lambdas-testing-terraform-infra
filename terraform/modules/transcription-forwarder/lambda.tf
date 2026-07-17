# Zero external dependencies -> zip just the handler (boto3 is in the runtime).
data "archive_file" "lambda" {
  type        = "zip"
  source_file = "${var.lambda_source_dir}/main.py"
  output_path = "${path.module}/.build/${local.name}-forwarder.zip"
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.name}-transcription-forwarder"
  retention_in_days = var.log_retention_days
  tags              = local.common_tags
}

resource "aws_lambda_function" "forwarder" {
  function_name    = "${local.name}-transcription-forwarder"
  role             = aws_iam_role.lambda.arn
  runtime          = var.lambda_runtime
  handler          = "main.lambda_handler"
  filename         = data.archive_file.lambda.output_path
  source_code_hash = data.archive_file.lambda.output_base64sha256
  timeout          = var.lambda_timeout
  memory_size      = var.lambda_memory

  environment {
    variables = {
      SSM_BASE       = local.ssm_base
      CLIENTS_PREFIX = local.ssm_base
      LANDING_PREFIX = local.landing_prefix
      AWS_ACCOUNT_ID = local.account_id
    }
  }

  depends_on = [
    aws_iam_role_policy.lambda,
    aws_cloudwatch_log_group.lambda,
  ]

  tags = local.common_tags
}

resource "aws_lambda_event_source_mapping" "sqs" {
  event_source_arn                   = aws_sqs_queue.transcriptions.arn
  function_name                      = aws_lambda_function.forwarder.arn
  batch_size                         = var.sqs_batch_size
  maximum_batching_window_in_seconds = 5
  function_response_types            = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = var.sqs_max_concurrency
  }
}
