# -*- mode: hcl -*-
# Datadog Monitors for SMB Inbox Triage
#
# Monitors:
#   1. Classification error rate  — alert on absolute error span count
#   2. p95 classification latency — alert on high tail latency
#   3. Human-review queue rate    — alert when too many emails routed to human queue
#   4. No classifications (dead-man) — alert if pipeline goes completely silent
#   5. LLM token budget             — warn when per-invocation token usage spikes
#
# Query format notes:
#   • Monitor queries must be single-line strings.
#   • clamp_min() and default_zero() are dashboard-only functions — not valid here.
#   • p95() is not a valid time-aggregation rollup; use avg() over a p95: metric.
#   • .as_rate() is not valid on custom/OTel metrics in monitor queries; omit it.
#   • Tag scope must be comma-separated (local.apm_service_scope uses commas).

locals {
  monitor_tags = concat(local.dd_tags, ["monitor_type:classification"])

  # Reusable notify block appended to monitor messages
  notify_block = local.notify_str != "" ? "\n\n${local.notify_str}" : ""
}

# ── 1. Classification Error Rate ─────────────────────────────────────────────
# Monitors the absolute count of errored classify_email spans per 5-minute window.
# Ratio-based alerting (errors/hits) requires composite monitors; absolute count
# is simpler, less fragile, and fires correctly at low traffic volumes.

resource "datadog_monitor" "classification_error_rate" {
  name    = "[${upper(var.env)}] SMB Inbox Triage — High Classification Error Rate"
  type    = "query alert"
  tags    = local.monitor_tags
  message = <<-EOT
    Classification errors exceeded threshold in the last 5 minutes.

    Service: ${var.service_name} | Environment: ${var.env}

    Possible causes: LLM API unavailable, JSON schema validation failure, function timeout.

    Check APM trace errors: https://app.datadoghq.com/apm/traces?query=service:${var.service_name}+env:${var.env}+status:error

    @is_alert ERROR COUNT: {{value}} errors/5m (threshold: ${var.error_rate_threshold_pct})${local.notify_block}
    @is_warning Classification error count is elevated.
    @is_recovery Classification error rate has returned to normal.
  EOT

  query = "sum(last_5m):sum:trace.classifier.classify_email.errors{${local.apm_service_scope}}.as_count() > ${var.error_rate_threshold_pct}"

  monitor_thresholds {
    warning  = var.error_rate_threshold_pct * 0.5
    critical = var.error_rate_threshold_pct
  }

  notify_no_data    = false
  renotify_interval = 60
  evaluation_delay  = 60
  include_tags      = true
  priority          = 2
}

# ── 2. p95 Classification Latency ────────────────────────────────────────────

resource "datadog_monitor" "classification_latency_p95" {
  name    = "[${upper(var.env)}] SMB Inbox Triage — High p95 Classification Latency"
  type    = "query alert"
  tags    = local.monitor_tags
  message = <<-EOT
    p95 classification latency exceeded threshold.

    Service: ${var.service_name} | Environment: ${var.env}

    Possible causes: LLM provider API slowness, Lambda cold-start spike, downstream routing delay.

    APM service page: https://app.datadoghq.com/apm/services/${var.service_name}/operations/classifier.classify_email?env=${var.env}

    @is_alert p95 LATENCY: {{value}}ms (threshold: ${var.p95_latency_threshold_ms}ms)${local.notify_block}
    @is_warning p95 classification latency is elevated.
    @is_recovery p95 classification latency has returned to normal.
  EOT

  # avg() is the correct time-aggregation rollup for a p95: distribution metric
  query = "avg(last_10m):p95:trace.classifier.classify_email{${local.apm_service_scope}} > ${var.p95_latency_threshold_ms}"

  monitor_thresholds {
    warning  = var.p95_latency_threshold_ms * 0.75
    critical = var.p95_latency_threshold_ms
  }

  notify_no_data    = false
  renotify_interval = 30
  evaluation_delay  = 60
  include_tags      = true
  priority          = 3
}

# ── 3. Human-Review Queue Rate ───────────────────────────────────────────────
# Monitors absolute count of emails routed to the human queue.
# Fires when the pipeline is consistently routing too many emails to humans,
# which indicates model confidence degradation or a prompt regression.

resource "datadog_monitor" "human_review_rate" {
  name    = "[${upper(var.env)}] SMB Inbox Triage — Elevated Human-Review Rate"
  type    = "query alert"
  tags    = concat(local.monitor_tags, ["monitor_type:model_health"])
  message = <<-EOT
    Human-review queue rate is elevated.

    Possible causes: model confidence degradation, prompt regression, unusual traffic pattern.

    Dashboard: https://app.datadoghq.com/dashboard/${datadog_dashboard.main.id}

    @is_alert HUMAN-REVIEW COUNT: {{value}} routed/15m (threshold: ${var.human_review_rate_threshold_pct})${local.notify_block}
    @is_recovery Human-review rate has returned to normal.
  EOT

  query = "sum(last_15m):sum:trace.router.dispatch{${local.apm_service_scope},routing.destination:human_queue}.as_count() > ${var.human_review_rate_threshold_pct}"

  monitor_thresholds {
    warning  = var.human_review_rate_threshold_pct * 0.7
    critical = var.human_review_rate_threshold_pct
  }

  notify_no_data    = false
  renotify_interval = 120
  evaluation_delay  = 120
  include_tags      = true
  priority          = 3
}

# ── 4. Pipeline Dead-Man (No Classifications) ────────────────────────────────

resource "datadog_monitor" "pipeline_no_data" {
  name    = "[${upper(var.env)}] SMB Inbox Triage — No Classifications Received"
  type    = "query alert"
  tags    = concat(local.monitor_tags, ["monitor_type:deadman"])
  message = <<-EOT
    No classification spans received in the last 30 minutes.

    The pipeline may be completely stopped: function not invoked, entrypoint crash, or webhook misconfiguration.

    Check Cloud Function errors: https://console.cloud.google.com/functions/list?project=your-gcp-project-id

    @is_alert PIPELINE SILENT: no classify_email spans for 30 min${local.notify_block}
    @is_recovery Pipeline is receiving classifications again.
  EOT

  query = "sum(last_30m):sum:trace.classifier.classify_email.hits{${local.apm_service_scope}}.as_count() < 1"

  monitor_thresholds {
    critical = 1
  }

  notify_no_data      = false
  require_full_window = false
  evaluation_delay    = 0
  include_tags        = true
  priority            = 1
}

# ── 5. LLM Token Budget Spike ────────────────────────────────────────────────

resource "datadog_monitor" "llm_token_spike" {
  name    = "[${upper(var.env)}] SMB Inbox Triage — LLM Token Usage Spike"
  type    = "query alert"
  tags    = concat(local.monitor_tags, ["monitor_type:cost_control"])
  message = <<-EOT
    Per-invocation LLM output token usage is abnormally high.

    Possible causes: email bodies not being truncated, prompt injection inflating input, model verbose output.

    Check the token usage panel on the dashboard and recent trace samples.

    @is_alert TOKEN SPIKE: {{value}} output tokens (threshold: 50)${local.notify_block}
    @is_recovery Token usage has returned to normal.
  EOT

  query = "avg(last_5m):sum:gen_ai.usage.output_tokens{${local.apm_service_scope}} > 50"

  monitor_thresholds {
    warning  = 30
    critical = 50
  }

  notify_no_data    = false
  renotify_interval = 60
  evaluation_delay  = 60
  include_tags      = true
  priority          = 4
}
