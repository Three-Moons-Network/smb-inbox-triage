# -*- mode: hcl -*-
# Datadog SLO — Classification Pipeline Availability
#
# SLO definition:
#   "Good events"   = classifier.classify_email spans that completed without error
#   "Total events"  = all classifier.classify_email spans
#   Target          = var.slo_target_pct (default 99.5%)
#   Window          = 7d rolling (Datadog SLO default)

resource "datadog_service_level_objective" "classification_availability" {
  name        = "SMB Inbox Triage — Classification Availability [${var.env}]"
  type        = "metric"
  description = <<-EOT
    Measures the percentage of inbound emails that are successfully classified
    end-to-end without error. A "failure" is any classifier.classify_email span
    that terminates with an error status (LLM API failure, JSON parse failure,
    or schema validation failure).

    Target: ${var.slo_target_pct}% over 7 days.
    Error budget: ${100 - var.slo_target_pct}% of invocations may fail.
  EOT

  tags = local.dd_tags

  query {
    # Good = total invocations minus errors
    numerator = join(" - ", [
      "sum:trace.classifier.classify_email.hits{${local.apm_service_scope}}.as_count()",
      "sum:trace.classifier.classify_email.errors{${local.apm_service_scope}}.as_count()",
    ])
    denominator = "sum:trace.classifier.classify_email.hits{${local.apm_service_scope}}.as_count()"
  }

  thresholds {
    timeframe = "7d"
    target    = var.slo_target_pct
    warning   = var.slo_warning_pct
  }

  thresholds {
    timeframe = "30d"
    target    = var.slo_target_pct
    warning   = var.slo_warning_pct
  }

  # Burn-rate alert: notify when error budget is consumed too quickly.
  # A 1h burn rate of 14.4x means the full 7d budget will be exhausted in ~12h.
  # Note: SLO burn-rate alerts require a Datadog SLO alert monitor (separate resource).
}

# ── SLO Error Budget Alerts ───────────────────────────────────────────────────
# burn_rate() queries are not available on all Datadog org tiers.
# These monitors use the error_budget() query instead, which is universally
# supported and gives equivalent coverage:
#   Critical: >75% of the 7-day error budget consumed (only 0.125% failures left)
#   Warning:  >50% of the 7-day error budget consumed (0.25% failures left)

resource "datadog_monitor" "slo_budget_critical" {
  name = "[${upper(var.env)}] SMB Inbox Triage — SLO Error Budget Critical"
  type = "slo alert"
  tags = concat(local.monitor_tags, ["monitor_type:slo"])

  message = <<-EOT
    SLO error budget is more than 75% consumed over the 7-day window.

    At this rate the pipeline will breach the ${var.slo_target_pct}% availability target
    before the window closes. Investigate classification errors immediately.

    Dashboard: https://app.datadoghq.com/dashboard/${datadog_dashboard.main.id}

    @is_alert SLO BUDGET CRITICAL: {{value}}% consumed (threshold: 75%)${local.notify_block}
    @is_recovery Error budget consumption has dropped back below 75%.
  EOT

  query = "error_budget(\"${datadog_service_level_objective.classification_availability.id}\").over(\"7d\") > 75"

  monitor_thresholds {
    critical = 75
    warning  = 50
  }

  notify_no_data = false
  priority       = 1
  include_tags   = true
}

resource "datadog_monitor" "slo_budget_warning" {
  name = "[${upper(var.env)}] SMB Inbox Triage — SLO Error Budget Warning"
  type = "slo alert"
  tags = concat(local.monitor_tags, ["monitor_type:slo"])

  message = <<-EOT
    SLO error budget is more than 50% consumed over the 30-day window.

    The pipeline is not in immediate danger but error rate should be reviewed
    before the window closes.

    @is_warning SLO BUDGET WARNING: {{value}}% consumed (threshold: 50%)${local.notify_block}
    @is_recovery Error budget consumption has dropped back below 50%.
  EOT

  query = "error_budget(\"${datadog_service_level_objective.classification_availability.id}\").over(\"30d\") > 50"

  monitor_thresholds {
    critical = 50
    warning  = 25
  }

  notify_no_data = false
  priority       = 3
  include_tags   = true
}
