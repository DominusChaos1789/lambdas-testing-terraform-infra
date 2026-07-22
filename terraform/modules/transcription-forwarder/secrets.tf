# Client credentials live in Secrets Manager and are managed OUTSIDE this module
# (created by the security/platform team). We only reference them to grant the
# Lambda read access, so the secret values never enter Terraform state.
#
# Full name: /<stack_id>/empatia/api/<client>-detalle, e.g.
#   /augusta-nexa-dev/empatia/api/bdo-detalle
# Expected JSON body:
#   { "client_id": "...", "client_secret": "...", "grant_type": "client_credentials",
#     "cypher_code": "<base64 AES-256 key>" }   # cypher_code only for encrypted clients
#
# The plan fails with a clear "secret not found" if a client is added before its
# secret exists.
data "aws_secretsmanager_secret" "client" {
  for_each = var.clients
  name     = local.client_secret_full[each.key]
}

# One routing parameter per client at /<stack_id>/empatia/api/<client>-detalle.
# URLs are split into host + path so environments can vary them independently.
resource "aws_ssm_parameter" "client" {
  for_each = var.clients

  name        = local.client_param_name[each.key]
  description = "EmpatIA routing for client ${each.key} -> ${each.value.endpoint_path}"
  type        = "String"
  value = jsonencode(merge(
    {
      token_url     = each.value.token_url
      token_path    = each.value.token_path
      api_url       = each.value.api_url
      api_path      = each.value.api_path
      endpoint_path = each.value.endpoint_path
      bucket_prefix = "${local.landing_prefix}${each.key}/"
      secret_name   = local.client_secret_rel[each.key]
      enabled       = each.value.enabled
    },
    each.value.enpoint_cypher_path == null ? {} : {
      enpoint_cypher_path = each.value.enpoint_cypher_path
    },
  ))
  tags = local.common_tags
}
