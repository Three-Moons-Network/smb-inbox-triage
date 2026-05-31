# -*- mode: hcl -*-
# Datadog provider configuration and shared locals.
#
# This module provisions:
#   • Datadog provider (credentials from variables, never hard-coded)
#   • LLM Observability + APM dashboard  (dashboard.tf)
#   • Monitors — error rate, latency, human-review rate  (monitors.tf)
#   • Availability SLO  (slo.tf)
#
# Authentication:
#   Set TF_VAR_dd_api_key and TF_VAR_dd_app_key in your shell or CI environment.
#   Do NOT pass these via terraform.tfvars or any committed file.
#
#   The `datadog` provider is configured by the CALLING root module (each cloud's
#   *-dev environment), not here. Keeping the provider in the root lets the
#   environment gate this module with `count`, so a deploy without Datadog
#   credentials simply skips monitor/SLO/dashboard creation.

terraform {
  required_providers {
    datadog = {
      source  = "DataDog/datadog"
      version = "~> 3.0"
    }
  }
}

locals {
  # Base tag list applied to all Datadog resources
  dd_tags = concat(
    [
      "service:${var.service_name}",
      "env:${var.env}",
      "team:platform-team",
    ],
    [for k, v in var.tags : "${k}:${v}"],
  )

  # Notification targets joined for monitor message blocks
  notify_targets = compact([
    var.notification_slack_channel,
    var.notification_pagerduty,
  ])
  notify_str = length(local.notify_targets) > 0 ? join(" ", local.notify_targets) : ""

  # APM trace query scope — matches spans emitted by tracing.py
  # Comma-separated: monitor validation API requires this format (space-sep is
  # accepted by dashboard APIs but rejected by /api/v1/monitor/validate).
  apm_service_scope = "service:${var.service_name},env:${var.env}"

  # Dashboard tags are restricted by this org to keys: team, ai.
  # service: and env: keys are blocked at the dashboard API level (monitors are unaffected).
  # Env and service context is already encoded in the dashboard title and widget queries.
  dashboard_tags = ["team:platform-team"]
}
