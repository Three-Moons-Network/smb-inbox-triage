terraform {
  required_providers {
    datadog = {
      source  = "DataDog/datadog"
      version = "~> 3.0"
    }
  }

  backend "azurerm" {
    resource_group_name  = "rg-tmn-tfstate"
    storage_account_name = "tmntfstateazure"
    container_name       = "tfstate"
    key                  = "inbox-triage/azure-dev/terraform.tfstate"
  }
}

module "inbox_triage_azure" {
  source = "../../modules/azure"

  project_name              = var.project_name
  env                       = var.env
  region                    = var.region
  llm_model_id              = var.llm_model_id
  slack_webhook_secret_name = var.slack_webhook_secret_name
  azure_openai_endpoint     = var.azure_openai_endpoint
  dd_site                   = var.dd_site
  dd_api_key                = var.dd_api_key
  azure_openai_api_key      = var.azure_openai_api_key
}

# ── Datadog monitoring (monitors, SLO, dashboard) ─────────────────────────────
# Created only when both Datadog keys are provided (TF_VAR_dd_api_key and
# TF_VAR_dd_app_key). Leave dd_app_key unset to deploy without Datadog monitoring.
provider "datadog" {
  api_key = var.dd_api_key
  app_key = var.dd_app_key
  api_url = "https://api.${var.dd_site}/"
}

module "inbox_triage_datadog" {
  count  = var.dd_api_key != "" && var.dd_app_key != "" ? 1 : 0
  source = "../../modules/datadog"

  dd_site      = var.dd_site
  service_name = var.project_name
  env          = var.env
}

output "webhook_url"  { value = module.inbox_triage_azure.webhook_url }
output "feedback_url" { value = module.inbox_triage_azure.feedback_url }

output "datastore_name" {
  description = "Cosmos DB classifications container name."
  value       = module.inbox_triage_azure.datastore_name
}

output "cosmos_account_endpoint" {
  description = "Cosmos DB account endpoint — used as the COSMOS_CONNECTION_STRING secret value."
  value       = module.inbox_triage_azure.cosmos_account_endpoint
}

output "app_insights_instrumentation_key" {
  description = "Application Insights instrumentation key."
  value       = module.inbox_triage_azure.app_insights_instrumentation_key
  sensitive   = true
}

output "acr_name" {
  description = "ACR name — pass to `az acr login --name`."
  value       = module.inbox_triage_azure.acr_name
}

output "acr_login_server" {
  description = "ACR login server — `docker push` target."
  value       = module.inbox_triage_azure.acr_login_server
}
