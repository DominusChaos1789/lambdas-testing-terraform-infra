data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  # Trailing segment of the stack id is the environment (augusta-nexa-dev -> dev).
  environment = reverse(split("-", var.stack_id))[0]

  landing_bucket = coalesce(var.landing_bucket, "${var.stack_id}-providers-landing")
  landing_prefix = var.landing_prefix

  # e.g. /augusta-nexa-dev/empatia/transcripciones/detalle
  ssm_base = "/${var.stack_id}/empatia/transcripciones/detalle"

  name        = "${var.stack_id}-empatia-detalle"
  bucket_arn  = "arn:aws:s3:::${local.landing_bucket}"
  account_id  = data.aws_caller_identity.current.account_id
  region      = data.aws_region.current.region
  client_keys = keys(var.clients)

  common_tags = merge(var.tags, {
    Stack       = var.stack_id
    Environment = local.environment
    ManagedBy   = "terraform"
    Module      = "transcription-forwarder"
  })
}
