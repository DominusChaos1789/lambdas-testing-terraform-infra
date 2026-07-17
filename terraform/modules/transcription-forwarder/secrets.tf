# Client credentials live in Secrets Manager and are managed OUTSIDE this module
# (created by the security/platform team). We only reference them to grant the
# Lambda read access, so the secret values never enter Terraform state.
#
# Expected JSON body, e.g. /augusta-nexa-dev/empatia/api/bdo_detalle:
#   { "client_id": "...", "client_secret": "...", "grant_type": "client_credentials" }
#
# The plan fails with a clear "secret not found" if a client is added here before
# its secret exists.
data "aws_secretsmanager_secret" "client" {
  for_each = var.clients
  name     = local.client_secret_names[each.key]
}

resource "aws_ssm_parameter" "client" {
  for_each = var.clients

  name        = "${local.ssm_base}/${each.key}"
  description = "EmpatIA routing for client ${each.key} -> ${each.value.endpoint_path}"
  type        = "String"
  value = jsonencode({
    token_url     = each.value.token_url
    api_base_url  = each.value.api_base_url
    endpoint_path = each.value.endpoint_path
    bucket_prefix = "${local.landing_prefix}${each.key}/"
    secret_name   = local.client_secret_names[each.key]
    enabled       = each.value.enabled
  })
  tags = local.common_tags
}
