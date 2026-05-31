variable "project_name" {
  type    = string
  default = "smb-inbox-triage"
}

variable "env" {
  type = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.env)
    error_message = "env must be dev, staging, or prod."
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "slack_webhook_secret_name" {
  type        = string
  description = "Name of the Secrets Manager secret holding the Slack webhook URL."
}

variable "llm_model_id" {
  type        = string
  default     = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
  description = "Bedrock model ID for the classifier."
}

variable "lambda_memory_mb" {
  type    = number
  default = 512
}

variable "lambda_timeout_seconds" {
  type    = number
  default = 30
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "tags" {
  type = map(string)
  default = {}
}

# ── Datadog variables ─────────────────────────────────────────────────────────

variable "dd_api_key_secret_arn" {
  description = <<-EOT
    ARN of the Secrets Manager secret containing the Datadog API key.
    The Lambda execution role is granted GetSecretValue on this ARN.
    Store the key value as a plain string (not JSON) in Secrets Manager.
    Set this via TF_VAR_dd_api_key_secret_arn — never hard-coded.
  EOT
  type    = string
  default = ""   # empty = DD Extension disabled; set in prod/staging env
}

variable "dd_site" {
  description = "Datadog site (e.g. datadoghq.com, datadoghq.eu)"
  type        = string
  default     = "datadoghq.com"
}

variable "dd_service" {
  description = "DD_SERVICE tag — must match the service name used in the Datadog module"
  type        = string
  default     = "smb-inbox-triage"
}

variable "dd_extension_version" {
  description = <<-EOT
    Datadog Lambda Extension layer version for arm64.
    Check https://github.com/DataDog/datadog-lambda-extension/releases for latest.
    ARN pattern: arn:aws:lambda:<region>:464622532012:layer:Datadog-Extension-ARM:<version>
  EOT
  type    = number
  default = 97  # v97 released 2026-05-15; pin and update deliberately
}
