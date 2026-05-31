# ── EventBridge — custom bus + routing rules ──────────────────────────────────

resource "aws_cloudwatch_event_bus" "main" {
  name = "${local.name_prefix}-bus"
}

# Dead letter queue for failed event delivery
resource "aws_sqs_queue" "dlq" {
  name                       = "${local.name_prefix}-dlq"
  message_retention_seconds  = 1209600  # 14 days
  visibility_timeout_seconds = 60
}

resource "aws_sqs_queue_policy" "dlq" {
  queue_url = aws_sqs_queue.dlq.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.dlq.arn
    }]
  })
}

# ── CloudWatch alarms on DLQ depth ───────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "dlq_depth" {
  alarm_name          = "${local.name_prefix}-dlq-depth"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Messages in the inbox-triage DLQ — routing failure"
  dimensions = {
    QueueName = aws_sqs_queue.dlq.name
  }
}

# ── Per-intent event rules ────────────────────────────────────────────────────
# Each rule matches on the `detail.intent` field published by the Lambda.
# Additional targets (SQS, SNS, Lambda) can be added per rule as needed.

locals {
  intent_rules = {
    sales_inquiry      = { description = "Sales inquiry → HubSpot pipeline" }
    support_request    = { description = "Support request → Linear issue" }
    billing_question   = { description = "Billing question → Slack #billing" }
    urgent_escalation  = { description = "Urgent escalation → #incidents + owner DM" }
    human_review       = { description = "Low-confidence / unknown → human queue" }
  }
}

resource "aws_cloudwatch_event_rule" "intent_rules" {
  for_each = local.intent_rules

  name           = "${local.name_prefix}-rule-${replace(each.key, "_", "-")}"
  event_bus_name = aws_cloudwatch_event_bus.main.name
  description    = each.value.description

  event_pattern = jsonencode({
    source      = ["smb-inbox-triage"]
    detail-type = ["EmailClassified"]
    detail = {
      intent = [each.key]
    }
  })
}
