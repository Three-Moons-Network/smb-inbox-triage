variable "project_name"              { type = string }
variable "env"                       { type = string }
variable "region"                    { type = string }
variable "gcp_project_id"            { type = string }
variable "llm_model_id"              { type = string }
variable "slack_webhook_secret_name" { type = string }
variable "dd_site" {
  type    = string
  default = "datadoghq.com"
}
variable "dd_api_key_secret_name" {
  type    = string
  default = "smb-inbox-triage-dd-api-key"
}

# Datadog monitoring provider (monitors/SLO/dashboard). Separate from the
# Secret-Manager-backed key the Cloud Run service reads: these raw keys configure
# the `datadog` provider. Set via TF_VAR_dd_api_key / TF_VAR_dd_app_key; leave
# empty to skip Datadog monitoring.
variable "dd_api_key" {
  type      = string
  default   = ""
  sensitive = true
}

variable "dd_app_key" {
  type      = string
  default   = ""
  sensitive = true
}

variable "developer_emails" {
  type    = list(string)
  default = []
}

variable "image_tag" {
  type    = string
  default = "latest"
}
