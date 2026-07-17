output "lambda_function_name" {
  value = aws_lambda_function.forwarder.function_name
}

output "lambda_function_arn" {
  value = aws_lambda_function.forwarder.arn
}

output "lambda_role_arn" {
  value = aws_iam_role.lambda.arn
}

output "queue_url" {
  value = aws_sqs_queue.transcriptions.id
}

output "queue_arn" {
  value = aws_sqs_queue.transcriptions.arn
}

output "dlq_url" {
  value = aws_sqs_queue.dlq.id
}

output "event_rule_arn" {
  value = aws_cloudwatch_event_rule.object_created.arn
}

output "ssm_client_params" {
  value = { for k, p in aws_ssm_parameter.client : k => p.name }
}

output "client_secret_arns" {
  value = { for k, s in data.aws_secretsmanager_secret.client : k => s.arn }
}
