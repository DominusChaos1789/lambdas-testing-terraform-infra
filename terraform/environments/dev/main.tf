module "transcription_forwarder" {
  source = "../../modules/transcription-forwarder"

  stack_id          = var.stack_id
  lambda_source_dir = "${path.module}/../../../lambdas/empatia-tipificaciones"

  clients     = var.clients
  kms_key_arn = var.kms_key_arn

  sqs_max_concurrency = 5
  lambda_timeout      = 30
}
