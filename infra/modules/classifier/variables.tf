# Shared variable definitions consumed by all three cloud modules.
# Each cloud module declares these same variables so callers use
# a consistent interface regardless of cloud target.

variable "project_name" {
  type        = string
  description = "Short name for this project, used in resource naming."
  default     = "smb-inbox-triage"
}

variable "env" {
  type        = string
  description = "Deployment environment: dev | staging | prod"
  validation {
    condition     = contains(["dev", "staging", "prod"], var.env)
    error_message = "env must be dev, staging, or prod."
  }
}

variable "region" {
  type        = string
  description = "Cloud region for primary deployment."
}

variable "slack_webhook_secret_name" {
  type        = string
  description = "Name of the secret in the cloud secrets store holding the Slack webhook URL."
}

variable "llm_model_id" {
  type        = string
  description = "LLM model identifier specific to the cloud provider."
}

variable "tags" {
  type        = map(string)
  description = "Common resource tags / labels applied to all resources."
  default     = {}
}
