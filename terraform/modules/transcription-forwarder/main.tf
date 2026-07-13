data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  name        = "${var.project}-${var.environment}"
  ssm_prefix  = "/nexa/empatia/${var.environment}"
  bucket_arn  = "arn:aws:s3:::${var.landing_bucket}"
  account_id  = data.aws_caller_identity.current.account_id
  region      = data.aws_region.current.region
  client_keys = keys(var.clients)

  common_tags = merge(var.tags, {
    Project     = var.project
    Environment = var.environment
    ManagedBy   = "terraform"
    Module      = "transcription-forwarder"
  })
}
