terraform {
  required_providers {
    datadog = {
      source  = "DataDog/datadog"
      version = "~> 3.0"
    }
  }

  backend "gcs" {
    bucket = "tmn-tfstate-gcp"
    prefix = "inbox-triage/gcp-dev"
  }
}

module "inbox_triage_gcp" {
  source = "../../modules/gcp"

  project_name              = var.project_name
  env                       = var.env
  region                    = var.region
  gcp_project_id            = var.gcp_project_id
  llm_model_id              = var.llm_model_id
  slack_webhook_secret_name = var.slack_webhook_secret_name
  dd_site                   = var.dd_site
  dd_api_key_secret_name    = var.dd_api_key_secret_name
  developer_emails          = var.developer_emails
  image_tag                 = var.image_tag
}

# ── Datadog monitoring (monitors, SLO, dashboard) ─────────────────────────────
# Created only when both Datadog keys are provided (TF_VAR_dd_api_key and
# TF_VAR_dd_app_key). Leave the keys unset to deploy without Datadog monitoring.
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

output "webhook_url"       { value = module.inbox_triage_gcp.webhook_url }
output "feedback_url"      { value = module.inbox_triage_gcp.feedback_url }
output "gmail_pubsub_topic" { value = module.inbox_triage_gcp.gmail_pubsub_topic }
