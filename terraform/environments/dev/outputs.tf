output "lambda_function_name" {
  value = module.transcription_forwarder.lambda_function_name
}

output "queue_url" {
  value = module.transcription_forwarder.queue_url
}

output "dlq_url" {
  value = module.transcription_forwarder.dlq_url
}

output "ssm_client_params" {
  value = module.transcription_forwarder.ssm_client_params
}

output "client_secret_arns" {
  value = module.transcription_forwarder.client_secret_arns
}
