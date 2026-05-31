variable "project_name"              { type = string }
variable "env"                       { type = string }
variable "region"                    { type = string }
variable "llm_model_id"              { type = string }
variable "slack_webhook_secret_name" { type = string }
variable "azure_openai_endpoint"     { type = string }
variable "dd_site"                   { type = string }
variable "dd_api_key" {
  type      = string
  sensitive = true
}
# Datadog application key for the monitoring provider (monitors/SLO/dashboard).
# Set via TF_VAR_dd_app_key. Leave empty to skip Datadog monitoring.
variable "dd_app_key" {
  type      = string
  default   = ""
  sensitive = true
}
variable "azure_openai_api_key" {
  type      = string
  sensitive = true
}
