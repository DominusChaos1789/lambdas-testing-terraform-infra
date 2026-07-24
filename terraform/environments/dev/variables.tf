variable "aws_region" {
  type    = string
  default = "us-east-2"
}

variable "stack_id" {
  type        = string
  default     = "augusta-nexa-dev"
  description = "Stack id; landing bucket becomes <stack_id>-providers-transit."
}

variable "kms_key_arn" {
  type        = string
  default     = null
  description = "CMK encrypting the client secrets (the augusta-nexa-dev key)."
}

variable "clients" {
  type = map(object({
    token_url           = string
    token_path          = string
    api_url             = string
    api_path            = string
    endpoint_path       = string
    enpoint_cypher_path = optional(string)
    secret_name         = optional(string)
    enabled             = optional(bool, true)
  }))
}
