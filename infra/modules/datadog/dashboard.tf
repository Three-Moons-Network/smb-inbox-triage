# -*- mode: hcl -*-
# SMB Inbox Triage — LLM Observability + APM Dashboard
#
# Widgets cover:
#   Row 1: Classification pipeline overview
#     • Total classifications (count)       • Error rate timeseries
#     • p50/p95 latency timeseries          • Requires-human rate
#   Row 2: LLM model metrics (gen_ai.* OTel attributes → DD LLM Observability)
#     • Token usage by model/cloud          • Model response time
#     • Intent distribution toplist
#   Row 3: Routing destinations
#     • Destinations breakdown              • Routing span latency
#   Row 4: Infrastructure
#     • Lambda invocations + errors (AWS)   • Trace map

resource "datadog_dashboard" "main" {
  title       = "SMB Inbox Triage — Classification Pipeline [${var.env}]"
  description = "End-to-end visibility: email classification, LLM calls, routing, and infrastructure health."
  layout_type = "ordered"
  tags        = local.dashboard_tags

  # ── Row 1: Pipeline Overview ────────────────────────────────────────────────

  widget {
    query_value_definition {
      title       = "Classifications (last 1h)"
      title_size  = "16"
      title_align = "left"
      autoscale   = true
      precision   = 0

      request {
        q          = "sum:trace.classifier.classify_email.hits{${local.apm_service_scope}}.as_count()"
        aggregator = "sum"
      }

      custom_unit = "emails"
    }
    widget_layout {
      x      = 0
      y      = 0
      width  = 3
      height = 2
    }
  }

  widget {
    timeseries_definition {
      title       = "Classification Error Rate"
      show_legend = true

      request {
        q            = "100 * sum:trace.classifier.classify_email.errors{${local.apm_service_scope}}.as_rate() / sum:trace.classifier.classify_email.hits{${local.apm_service_scope}}.as_rate()"
        display_type = "line"
        style {
          palette    = "warm"
          line_type  = "solid"
          line_width = "normal"
        }
      }

      yaxis {
        min   = "0"
        max   = "100"
        label = "error %"
      }

      marker {
        value        = "y = ${var.error_rate_threshold_pct}"
        display_type = "error dashed"
        label        = "alert threshold"
      }
    }
    widget_layout {
      x      = 3
      y      = 0
      width  = 5
      height = 2
    }
  }

  widget {
    timeseries_definition {
      title       = "Classification Latency (p50 / p95)"
      show_legend = true

      request {
        q            = "p50:trace.classifier.classify_email{${local.apm_service_scope}}"
        display_type = "line"
        style { palette = "blue" }
        metadata {
          expression = "p50:trace.classifier.classify_email{${local.apm_service_scope}}"
          alias_name = "p50"
        }
      }

      request {
        q            = "p95:trace.classifier.classify_email{${local.apm_service_scope}}"
        display_type = "line"
        style { palette = "orange" }
        metadata {
          expression = "p95:trace.classifier.classify_email{${local.apm_service_scope}}"
          alias_name = "p95"
        }
      }

      yaxis { label = "ms" }

      marker {
        value        = "y = ${var.p95_latency_threshold_ms}"
        display_type = "warning dashed"
        label        = "p95 alert threshold"
      }
    }
    widget_layout {
      x      = 8
      y      = 0
      width  = 4
      height = 2
    }
  }

  widget {
    query_value_definition {
      title       = "Requires-Human Rate (last 1h)"
      title_size  = "16"
      title_align = "left"
      autoscale   = true
      precision   = 1

      request {
        q = join(" / ", [
          "sum:trace.router.dispatch{${local.apm_service_scope},routing.destination:human_queue}.as_count()",
          "sum:trace.classifier.classify_email.hits{${local.apm_service_scope}}.as_count()",
        ])
        aggregator = "avg"
        conditional_formats {
          comparator = ">="
          value      = var.human_review_rate_threshold_pct / 100
          palette    = "red_on_white"
        }
        conditional_formats {
          comparator = "<"
          value      = var.human_review_rate_threshold_pct / 100
          palette    = "green_on_white"
        }
      }
    }
    widget_layout {
      x      = 0
      y      = 2
      width  = 3
      height = 2
    }
  }

  # ── Row 2: LLM Model Metrics ─────────────────────────────────────────────────

  widget {
    timeseries_definition {
      title       = "LLM Token Usage — Input vs Output"
      show_legend = true

      request {
        q            = "sum:gen_ai.usage.input_tokens{${local.apm_service_scope}} by {gen_ai.request.model,cloud}.as_rate()"
        display_type = "bars"
        style { palette = "blue" }
        metadata {
          expression = "sum:gen_ai.usage.input_tokens{${local.apm_service_scope}} by {gen_ai.request.model,cloud}.as_rate()"
          alias_name = "input tokens/s"
        }
      }

      request {
        q            = "sum:gen_ai.usage.output_tokens{${local.apm_service_scope}} by {gen_ai.request.model,cloud}.as_rate()"
        display_type = "bars"
        style { palette = "green" }
        metadata {
          expression = "sum:gen_ai.usage.output_tokens{${local.apm_service_scope}} by {gen_ai.request.model,cloud}.as_rate()"
          alias_name = "output tokens/s"
        }
      }

      yaxis { label = "tokens/s" }
    }
    widget_layout {
      x      = 3
      y      = 2
      width  = 5
      height = 2
    }
  }

  widget {
    timeseries_definition {
      title       = "LLM Span Latency by Cloud (p95)"
      show_legend = true

      request {
        q            = "p95:trace.gen_ai.bedrock.converse{${local.apm_service_scope}}"
        display_type = "line"
        style { palette = "orange" }
        metadata {
          expression = "p95:trace.gen_ai.bedrock.converse{${local.apm_service_scope}}"
          alias_name = "Bedrock p95"
        }
      }

      request {
        q            = "p95:trace.gen_ai.azure_openai.chat{${local.apm_service_scope}}"
        display_type = "line"
        style { palette = "purple" }
        metadata {
          expression = "p95:trace.gen_ai.azure_openai.chat{${local.apm_service_scope}}"
          alias_name = "Azure OpenAI p95"
        }
      }

      request {
        q            = "p95:trace.gen_ai.vertex_ai.generate{${local.apm_service_scope}}"
        display_type = "line"
        style { palette = "green" }
        metadata {
          expression = "p95:trace.gen_ai.vertex_ai.generate{${local.apm_service_scope}}"
          alias_name = "Vertex AI p95"
        }
      }

      yaxis { label = "ms" }
    }
    widget_layout {
      x      = 8
      y      = 2
      width  = 4
      height = 2
    }
  }

  widget {
    toplist_definition {
      title = "Intent Distribution (last 4h)"

      request {
        q = "sum:trace.classifier.classify_email.hits{${local.apm_service_scope}} by {classification.intent}.as_count()"
        style {
          palette = "dog_classic"
        }
      }
    }
    widget_layout {
      x      = 0
      y      = 4
      width  = 4
      height = 2
    }
  }

  # ── Row 3: Routing Destinations ──────────────────────────────────────────────

  widget {
    toplist_definition {
      title = "Routing Destinations (last 4h)"

      request {
        q = "sum:trace.router.dispatch{${local.apm_service_scope}} by {routing.destination}.as_count()"
        style { palette = "dog_classic" }
      }
    }
    widget_layout {
      x      = 4
      y      = 4
      width  = 4
      height = 2
    }
  }

  widget {
    timeseries_definition {
      title       = "Routing Latency by Destination (p95)"
      show_legend = true

      request {
        q            = "p95:trace.router.slack.send{${local.apm_service_scope}}"
        display_type = "line"
        metadata {
          expression = "p95:trace.router.slack.send{${local.apm_service_scope}}"
          alias_name = "Slack"
        }
      }

      request {
        q            = "p95:trace.router.hubspot.create{${local.apm_service_scope}}"
        display_type = "line"
        metadata {
          expression = "p95:trace.router.hubspot.create{${local.apm_service_scope}}"
          alias_name = "HubSpot"
        }
      }

      request {
        q            = "p95:trace.router.linear.create_issue{${local.apm_service_scope}}"
        display_type = "line"
        metadata {
          expression = "p95:trace.router.linear.create_issue{${local.apm_service_scope}}"
          alias_name = "Linear"
        }
      }

      yaxis { label = "ms" }
    }
    widget_layout {
      x      = 8
      y      = 4
      width  = 4
      height = 2
    }
  }

  # ── Row 4: Infrastructure ────────────────────────────────────────────────────

  widget {
    timeseries_definition {
      title       = "Lambda Invocations + Errors (AWS)"
      show_legend = true

      request {
        q            = "sum:aws.lambda.invocations{service:${var.service_name},env:${var.env}}.as_count()"
        display_type = "bars"
        style { palette = "blue" }
        metadata {
          expression = "sum:aws.lambda.invocations{service:${var.service_name},env:${var.env}}.as_count()"
          alias_name = "invocations"
        }
      }

      request {
        q            = "sum:aws.lambda.errors{service:${var.service_name},env:${var.env}}.as_count()"
        display_type = "bars"
        style { palette = "red" }
        metadata {
          expression = "sum:aws.lambda.errors{service:${var.service_name},env:${var.env}}.as_count()"
          alias_name = "errors"
        }
      }
    }
    widget_layout {
      x      = 0
      y      = 6
      width  = 6
      height = 2
    }
  }

  widget {
    trace_service_definition {
      title             = "Service Map — ${var.service_name}"
      env               = var.env
      service           = var.service_name
      span_name         = "flask.request"
      show_breakdown    = true
      show_distribution = true
      show_errors       = true
      show_hits         = true
      show_latency      = true
      show_resource_list = false
      size_format       = "medium"
      display_format    = "three_column"
    }
    widget_layout {
      x      = 6
      y      = 6
      width  = 6
      height = 2
    }
  }
}
