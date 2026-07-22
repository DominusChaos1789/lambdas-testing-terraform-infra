# The handler needs `cryptography` (native wheels), so build.py fetches the
# Linux (Amazon Linux) wheels into build/ and copies main.py alongside. It runs
# on apply whenever main.py or the deps change.
resource "null_resource" "build" {
  triggers = {
    main         = filemd5("${var.lambda_source_dir}/main.py")
    requirements = filemd5("${var.lambda_source_dir}/requirements.txt")
    build        = filemd5("${var.lambda_source_dir}/build.py")
  }

  provisioner "local-exec" {
    command = "${var.build_python} \"${var.lambda_source_dir}/build.py\""
  }
}

# depends_on defers this read until after the build runs on apply.
data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${var.lambda_source_dir}/build"
  output_path = "${path.module}/.build/${local.name}-forwarder.zip"
  depends_on  = [null_resource.build]
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
      STACK_ID       = var.stack_id
      PARAM_PREFIX   = local.api_prefix
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
