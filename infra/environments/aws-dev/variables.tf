variable "project_name"              { type = string }
variable "env"                       { type = string }
variable "region"                    { type = string }
variable "llm_model_id"              { type = string }
variable "slack_webhook_secret_name" { type = string }

# ── Datadog (optional — set via TF_VAR_ env vars) ─────────────────────────────
variable "dd_api_key_secret_arn" {
  type        = string
  default     = ""
  description = <<-EOT
    ARN of the Secrets Manager secret containing the Datadog API key.
    Set via: $env:TF_VAR_dd_api_key_secret_arn = "arn:aws:secretsmanager:..."
    Never hard-code — the ARN contains the account ID.
    Leave empty to disable the Datadog Lambda Extension.
  EOT
}

variable "dd_site" {
  type        = string
  default     = "datadoghq.com"
  description = "Datadog site. Must match the site your account lives on (e.g. us5.datadoghq.com)."
}

# ── Datadog monitoring provider (monitors/SLO/dashboard) ──────────────────────
# Separate from dd_api_key_secret_arn above: that ARN feeds the Lambda Extension;
# these raw keys configure the `datadog` provider that creates the monitors.
# Set both via TF_VAR_dd_api_key / TF_VAR_dd_app_key. Leave empty to skip monitoring.
variable "dd_api_key" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Datadog API key for the monitoring provider. Set via TF_VAR_dd_api_key."
}

variable "dd_app_key" {
  type        = string
  default     = ""
  sensitive   = true
  description = "Datadog application key for the monitoring provider. Set via TF_VAR_dd_app_key."
}
