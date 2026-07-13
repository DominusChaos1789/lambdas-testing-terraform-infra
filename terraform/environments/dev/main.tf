module "transcription_forwarder" {
  source = "../../modules/transcription-forwarder"

  stack_id          = var.stack_id
  lambda_source_dir = "${path.module}/../../../lambdas/empatia-tipificaciones"

  keycloak_config = var.keycloak_config
  clients         = var.clients

  sqs_max_concurrency = 5
  lambda_timeout      = 30
}
