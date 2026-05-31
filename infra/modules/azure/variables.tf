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
  type        = string
  default     = "eastus"
  description = "Azure region."
}

variable "slack_webhook_secret_name" {
  type        = string
  description = "Name of the Key Vault secret holding the Slack webhook URL."
}

variable "llm_model_id" {
  type        = string
  default     = "gpt-4o-mini"
  description = "Azure OpenAI deployment name."
}

variable "azure_openai_endpoint" {
  type        = string
  description = "HTTPS endpoint for the Azure OpenAI resource."
}

variable "tags" {
  type    = map(string)
  default = {}
}

# ── Datadog (OTLP direct intake — no agent on Azure Flex Consumption) ─────────
#
# Azure Functions on Flex Consumption does not support sidecar containers or a
# Datadog site extension, so the Function App ships OTLP/HTTPS directly to
# Datadog's OTLP intake endpoint (https://otlp.<dd_site>). Auth is via a
# DD-API-KEY header sourced from Key Vault at runtime.

variable "dd_enabled" {
  type        = bool
  default     = true
  description = "When true, wire Datadog OTLP direct-intake env vars into both Function Apps."
}

variable "dd_site" {
  type        = string
  default     = "datadoghq.com"
  description = "Datadog site domain (datadoghq.com, datadoghq.eu, us3.datadoghq.com, etc)."
}

variable "dd_service" {
  type        = string
  default     = "smb-inbox-triage"
  description = "DD_SERVICE tag value."
}

variable "dd_version" {
  type        = string
  default     = "1.0.0"
  description = "DD_VERSION tag value — bump via CI on deploy."
}

variable "dd_api_key_secret_name" {
  type        = string
  default     = "datadog-api-key"
  description = "Key Vault secret name holding the Datadog API key (used by Flex/legacy paths)."
}

variable "dd_api_key" {
  type        = string
  sensitive   = true
  description = "Datadog API key — passed directly into the Container App secret store. Set via TF_VAR_dd_api_key env var; don't commit."
}

# ── Azure OpenAI key (literal, not a KV reference) ────────────────────────────
#
# Container Apps does NOT resolve the @Microsoft.KeyVault(SecretUri=...) syntax
# the way Function Apps do — the env var ends up containing the literal string,
# and Azure OpenAI rejects it with AuthenticationError. We pass the key directly
# instead. Set via TF_VAR_azure_openai_api_key env var:
#
#   $env:TF_VAR_azure_openai_api_key = (az keyvault secret show --vault-name
#       your-key-vault-name --name azure-openai-key --query value -o tsv)

variable "azure_openai_api_key" {
  type        = string
  sensitive   = true
  description = "Azure OpenAI API key — passed directly into the classifier Container App. Set via TF_VAR_azure_openai_api_key env var."
}
