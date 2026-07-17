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
  default     = "transacciones/empatia/transcripciones/detalle/"
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
    token_url     = string
    api_base_url  = string
    endpoint_path = string
    secret_name   = optional(string)
    enabled       = optional(bool, true)
  }))
  description = <<-EOT
    Map of client_key => config. The client_key is the S3 folder under the landing
    prefix (e.g. "BDO"); endpoint_path is the last segment of the API URL (e.g.
    "banco_occ") -- they are intentionally decoupled.

    Credentials are NOT stored here: each client reads them from a Secrets Manager
    secret. secret_name defaults to /<stack_id>/empatia/api/<lowercase key>_detalle
    (e.g. BDO -> /augusta-nexa-dev/empatia/api/bdo_detalle). The secret must already
    exist; this module only reads it.
  EOT
}

variable "kms_key_arn" {
  type        = string
  default     = null
  description = "CMK ARN encrypting the client secrets (e.g. the augusta-nexa-dev key)."
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
