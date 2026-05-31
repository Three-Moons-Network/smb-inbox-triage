# -*- mode: hcl -*-
# Datadog module variables
#
# NOTE: dd_api_key / dd_app_key are NOT module inputs. The `datadog` provider is
# configured in the calling root module (each *-dev environment), which lets the
# module be count-gated. This module only needs dd_site (for the dashboard URL)
# plus the service/threshold settings below.

variable "dd_site" {
  description = "Datadog site (e.g. datadoghq.com, datadoghq.eu, us3.datadoghq.com)"
  type        = string
  default     = "datadoghq.com"
}

variable "service_name" {
  description = "DD service name — must match DD_SERVICE env var in Lambda / Cloud Functions"
  type        = string
  default     = "smb-inbox-triage"
}

variable "env" {
  description = "Deployment environment (prod, staging, dev)"
  type        = string
}

variable "aws_region" {
  description = "AWS region where Lambda functions are deployed (used in monitor queries)"
  type        = string
  default     = "us-east-1"
}

variable "error_rate_threshold_pct" {
  description = "Alert threshold: classification error rate percent (0-100)"
  type        = number
  default     = 5.0
}

variable "p95_latency_threshold_ms" {
  description = "Alert threshold: p95 classification latency in milliseconds"
  type        = number
  default     = 8000
}

variable "human_review_rate_threshold_pct" {
  description = "Alert threshold: percent of emails routed to human review queue"
  type        = number
  default     = 30.0
}

variable "slo_target_pct" {
  description = "Classification availability SLO target (e.g. 99.5)"
  type        = number
  default     = 99.5
}

variable "slo_warning_pct" {
  description = "Classification availability SLO warning threshold"
  type        = number
  default     = 99.9
}

variable "notification_slack_channel" {
  description = "Slack channel handle for monitor alerts (e.g. @slack-ops-alerts)"
  type        = string
  default     = ""
}

variable "notification_pagerduty" {
  description = "PagerDuty service handle for critical alerts (e.g. @pagerduty-oncall)"
  type        = string
  default     = ""
}

variable "tags" {
  description = "Additional tags applied to all Datadog resources"
  type        = map(string)
  default     = {}
}
