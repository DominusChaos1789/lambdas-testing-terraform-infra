variable "stack_id" {
  type        = string
  description = "Stack identifier, e.g. augusta-nexa-dev. The trailing segment is the environment."
}

variable "landing_bucket" {
  type        = string
  default     = null
  description = "Landing bucket. Defaults to \"<stack_id>-providers-landing\" when null."
}

variable "landing_prefix" {
  type        = string
  default     = "transacciones/empatia/api/transcripciones/detalle/"
  description = "Fixed S3 key prefix providers replicate into; client key is the next segment."
}

variable "manage_bucket_notification" {
  type        = bool
  default     = true
  description = <<-EOT
    Whether this module enables EventBridge notifications on the landing bucket.
    aws_s3_bucket_notification is authoritative for the bucket: set to false if the
    bucket's notification config is managed elsewhere and enable EventBridge there.
  EOT
}

variable "clients" {
  type = map(object({
    api_base_url  = string
    endpoint_path = string
    enabled       = optional(bool, true)
  }))
  description = <<-EOT
    Map of client_key => endpoint config. The client_key is the first path segment
    of the S3 object (e.g. "banco_occ/2026/07/13/file.json"). Onboard a new endpoint
    by adding an entry here -- no Lambda code change required.
  EOT
}

variable "keycloak_config" {
  type = object({
    token_url     = string
    client_id     = string
    client_secret = string
  })
  sensitive   = true
  description = "Shared Keycloak client_credentials config, stored as a SecureString."
}

variable "kms_key_arn" {
  type        = string
  default     = null
  description = "Optional CMK ARN for the SecureString. Defaults to the aws/ssm managed key."
}

variable "lambda_source_dir" {
  type        = string
  description = "Path to the Lambda source directory containing main.py."
}

variable "lambda_runtime" {
  type    = string
  default = "python3.12"
}

variable "lambda_timeout" {
  type    = number
  default = 30
}

variable "lambda_memory" {
  type    = number
  default = 256
}

variable "sqs_batch_size" {
  type    = number
  default = 10
}

variable "sqs_max_concurrency" {
  type        = number
  default     = 5
  description = "Max concurrent Lambda invocations from SQS (throttles the downstream API)."
}

variable "max_receive_count" {
  type        = number
  default     = 5
  description = "Deliveries attempted before a message is moved to the DLQ."
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "tags" {
  type    = map(string)
  default = {}
}
