variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "stack_id" {
  type        = string
  default     = "augusta-nexa-dev"
  description = "Stack id; landing bucket becomes <stack_id>-providers-landing."
}

variable "keycloak_config" {
  type = object({
    token_url     = string
    client_id     = string
    client_secret = string
  })
  sensitive   = true
  description = "Provide via TF_VAR_keycloak_config or a non-committed .tfvars/secret manager."
}

variable "clients" {
  type = map(object({
    api_base_url  = string
    endpoint_path = string
    enabled       = optional(bool, true)
  }))
}
