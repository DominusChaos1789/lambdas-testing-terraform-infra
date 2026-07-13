# Shared Keycloak client_credentials, encrypted at rest (JSON body).
resource "aws_ssm_parameter" "keycloak" {
  name        = "${local.ssm_base}/keycloak"
  description = "Shared Keycloak client_credentials for the EmpatIA API"
  type        = "SecureString"
  key_id      = var.kms_key_arn
  value       = jsonencode(var.keycloak_config)
  tags        = local.common_tags
}

# One config parameter per client/endpoint (JSON body). Adding a client to the
# var.clients map creates a new parameter here -- no Lambda change needed.
resource "aws_ssm_parameter" "client" {
  for_each = var.clients

  name        = "${local.ssm_base}/${each.key}"
  description = "EmpatIA endpoint routing for client ${each.key}"
  type        = "String"
  value = jsonencode({
    api_base_url  = each.value.api_base_url
    endpoint_path = each.value.endpoint_path
    enabled       = each.value.enabled
  })
  tags = local.common_tags
}
