terraform {
  required_providers {
    datadog = {
      source  = "DataDog/datadog"
      version = "~> 3.0"
    }
  }

  backend "s3" {
    bucket         = "example-terraform-state"
    key            = "inbox-triage/aws-dev/terraform.tfstate"
    region         = "us-west-2"       # state bucket lives in us-west-2
    dynamodb_table = "TerraformStateLock"
    encrypt        = true
  }
}

module "inbox_triage_aws" {
  source = "../../modules/aws"

  project_name              = var.project_name
  env                       = var.env
  region                    = var.region
  llm_model_id              = var.llm_model_id
  slack_webhook_secret_name = var.slack_webhook_secret_name
  lambda_memory_mb          = 512
  lambda_timeout_seconds    = 30
  log_retention_days        = 14

  # Datadog Lambda Extension — set TF_VAR_dd_api_key_secret_arn before apply.
  # Leave empty (default) to deploy without the extension.
  dd_api_key_secret_arn = var.dd_api_key_secret_arn

  # Datadog site — MUST match the site your DD account lives on.
  # Your account is us5.datadoghq.com. The module default is datadoghq.com (US1),
  # which causes the Extension to ship to the wrong endpoint and all data is lost.
  dd_site = var.dd_site
}

# ── Datadog monitoring (monitors, SLO, dashboard) ─────────────────────────────
# Created only when both Datadog keys are provided (TF_VAR_dd_api_key and
# TF_VAR_dd_app_key). The provider lives here in the root so the module can be
# count-gated; leave the keys unset to deploy without Datadog monitoring.
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

output "webhook_url"           { value = module.inbox_triage_aws.webhook_url }
output "feedback_url"          { value = module.inbox_triage_aws.feedback_url }
output "classifier_lambda_arn" { value = module.inbox_triage_aws.classifier_lambda_arn }
output "datastore_name"        { value = module.inbox_triage_aws.datastore_name }
output "event_bus_name"        { value = module.inbox_triage_aws.event_bus_name }
output "dlq_url"               { value = module.inbox_triage_aws.dlq_url }
