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
  default = "us-central1"
}

variable "gcp_project_id" {
  type        = string
  description = "GCP project ID."
}

variable "slack_webhook_secret_name" {
  type        = string
  description = "Name of the Secret Manager secret holding the Slack webhook URL."
}

variable "llm_model_id" {
  type    = string
  default = "gemini-1.5-flash-002"
}

variable "function_memory_mb" {
  type    = number
  default = 512
}

variable "function_timeout_seconds" {
  type    = number
  default = 60
}

variable "labels" {
  type    = map(string)
  default = {}
}

variable "dd_site" {
  type        = string
  description = "Datadog site (e.g. datadoghq.com, datadoghq.eu, us3.datadoghq.com)"
  default     = "datadoghq.com"
}

variable "dd_api_key_secret_name" {
  type        = string
  description = "Secret Manager secret name holding the Datadog API key."
  default     = "smb-inbox-triage-dd-api-key"
}

variable "developer_emails" {
  type        = list(string)
  description = "IAM user emails granted secretAccessor on all module secrets (dev/debugging access). Empty in prod."
  default     = []
}

variable "image_tag" {
  type        = string
  description = "Docker image tag to deploy (e.g. git SHA, semver, or 'latest'). Build the image first with cloudbuild.yaml."
  default     = "latest"
}
