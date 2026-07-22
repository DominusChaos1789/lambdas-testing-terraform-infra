data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  # Trailing segment of the stack id is the environment (augusta-nexa-dev -> dev).
  environment = reverse(split("-", var.stack_id))[0]

  landing_bucket = coalesce(var.landing_bucket, "${var.stack_id}-providers-landing")
  landing_prefix = var.landing_prefix

  # Per-client parameters and secrets share the suffix "<client>-detalle" under
  # /<stack>/empatia/api. BDO -> /augusta-nexa-dev/empatia/api/bdo-detalle.
  api_prefix = "/${var.stack_id}/empatia/api"

  name        = "${var.stack_id}-empatia-detalle"
  bucket_arn  = "arn:aws:s3:::${local.landing_bucket}"
  account_id  = data.aws_caller_identity.current.account_id
  region      = data.aws_region.current.region
  client_keys = keys(var.clients)

  # Parameter name and the (environment-relative) secret_name stored inside it.
  client_param_name = {
    for k in local.client_keys : k => "${local.api_prefix}/${lower(k)}-detalle"
  }
  client_secret_rel = {
    for k, v in var.clients :
    k => coalesce(v.secret_name, "empatia/api/${lower(k)}-detalle")
  }
  client_secret_full = {
    for k in local.client_keys :
    k => "/${var.stack_id}/${local.client_secret_rel[k]}"
  }

  common_tags = merge(var.tags, {
    Stack       = var.stack_id
    Environment = local.environment
    ManagedBy   = "terraform"
    Module      = "transcription-forwarder"
  })
}
