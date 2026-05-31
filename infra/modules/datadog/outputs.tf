# -*- mode: hcl -*-

output "dashboard_id" {
  description = "Datadog dashboard ID for the classification pipeline overview"
  value       = datadog_dashboard.main.id
}

output "dashboard_url" {
  description = "Direct URL to the Datadog dashboard"
  value       = "https://app.${var.dd_site}/dashboard/${datadog_dashboard.main.id}"
}

output "slo_id" {
  description = "Datadog SLO resource ID for classification availability"
  value       = datadog_service_level_objective.classification_availability.id
}

output "monitor_ids" {
  description = "Map of monitor name → Datadog monitor ID"
  value = {
    classification_error_rate = datadog_monitor.classification_error_rate.id
    classification_latency    = datadog_monitor.classification_latency_p95.id
    human_review_rate         = datadog_monitor.human_review_rate.id
    pipeline_no_data          = datadog_monitor.pipeline_no_data.id
    llm_token_spike           = datadog_monitor.llm_token_spike.id
    slo_budget_critical       = datadog_monitor.slo_budget_critical.id
    slo_budget_warning        = datadog_monitor.slo_budget_warning.id
  }
}
