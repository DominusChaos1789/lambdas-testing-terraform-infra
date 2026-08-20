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
  default     = "external/datanexa/transacciones/empatia/transcripciones/"
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
    token_url           = string           # host, e.g. https://login-server-staging.nexabpo.com
    token_path          = string           # auth/realms/nexa/protocol/openid-connect/token
    api_url             = string           # host, e.g. https://nexa-empatia-staging.nexabpo.com
    api_path            = string           # transcription/api/tipificaciones
    endpoint_path       = optional(string) # banco_occ (omit to send only the encrypted copy)
    enpoint_cypher_path = optional(string) # bboc_encrip (sic - matches config key)
    secret_name         = optional(string) # relative, e.g. empatia/api/bdo-detalle
    enabled             = optional(bool, true)
  }))
  description = <<-EOT
    Map of client_key => config. The client_key is the S3 folder under the landing
    prefix (e.g. "BDO") and maps to the parameter/secret "<lower key>-detalle".
    endpoint_path / enpoint_cypher_path are the last URL segments (decoupled from
    the folder name).

    Credentials are NOT stored here: each client reads them from a Secrets Manager
    secret that must already exist. secret_name is environment-relative (no stack
    prefix) and defaults to empatia/api/<lower key>-detalle; the Lambda prepends
    /<stack_id>/ at runtime.
  EOT
}

variable "kms_key_arn" {
  type        = string
  default     = null
  description = "CMK ARN encrypting the client secrets (e.g. the augusta-nexa-dev key)."
}

variable "lambda_source_dir" {
  type        = string
  description = "Path to the Lambda source directory containing main.py and build.py."
}

variable "build_python" {
  type        = string
  default     = "python"
  description = "Python executable used to run build.py (e.g. python, python3)."
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
