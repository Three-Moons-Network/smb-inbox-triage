locals {
  name_prefix = "${var.project_name}-${var.env}"
  common_tags = merge(
    {
      project = var.project_name
      env     = var.env
      cloud   = "aws"
      owner   = "platform-team"
    },
    var.tags,
  )
}

provider "aws" {
  region = var.region
  default_tags { tags = local.common_tags }
}

# T9: used to scope CloudWatch Logs ARN to the actual account, not wildcard
data "aws_caller_identity" "current" {}

# ── Build Lambda package (source + Python dependencies) ───────────────────────
#
# Terraform's archive_file only zips files — it cannot install Python packages.
# This null_resource runs scripts/build_lambda.py which:
#   1. pip-installs pydantic (arm64/manylinux wheel) and httpx into .build/package/
#   2. Copies src/ contents into .build/package/
#
# archive_file then zips .build/package/ → .build/lambda.zip.
# The trigger hash covers every .py file in src/ so the build reruns automatically
# when source changes.

resource "null_resource" "lambda_build" {
  triggers = {
    src_hash = sha256(join("", [
      for f in sort(fileset("${path.module}/../../../src", "**/*.py")) :
      filemd5("${path.module}/../../../src/${f}")
    ]))
  }

  provisioner "local-exec" {
    # Python is required to develop this project so it is always available.
    command     = "python scripts/build_lambda.py"
    working_dir = "${path.module}/../../.."
  }
}

# ── Package Lambda zip ────────────────────────────────────────────────────────

data "archive_file" "lambda_zip" {
  depends_on  = [null_resource.lambda_build]
  type        = "zip"
  source_dir  = "${path.module}/../../../.build/package"
  output_path = "${path.module}/../../../.build/lambda.zip"
}

# ── Secrets Manager — Slack webhook URL ──────────────────────────────────────

data "aws_secretsmanager_secret" "slack_webhook" {
  name = var.slack_webhook_secret_name
}

# ── IAM role for Lambda ───────────────────────────────────────────────────────

resource "aws_iam_role" "lambda_exec" {
  name = "${local.name_prefix}-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "lambda_policy" {
  name = "${local.name_prefix}-lambda-policy"
  role = aws_iam_role.lambda_exec.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "BedrockInvoke"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        # Once model access is enabled in an account, AWS scopes the system-defined
        # cross-region inference profile ARN to that account.  list-inference-profiles
        # confirms the actual ARN is:
        #   arn:aws:bedrock:<region>:<account_id>:inference-profile/<id>
        # Using "::" (no account) does NOT match and causes AccessDeniedException.
        #
        # We also need the underlying foundation model ARN in every US region the
        # profile may route to (us-east-1, us-west-2, us-east-2).
        Resource = [
          # Account-scoped inference profile ARN (verified via list-inference-profiles)
          "arn:aws:bedrock:${var.region}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.llm_model_id}",
          # Foundation models in each US region the profile can fan out to
          "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
          "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
          "arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
        ]
      },
      {
        Sid      = "DynamoDBWrite"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem"]
        Resource = [
          aws_dynamodb_table.classifications.arn,
          aws_dynamodb_table.feedback.arn,
        ]
      },
      {
        Sid      = "EventBridgePut"
        Effect   = "Allow"
        Action   = ["events:PutEvents"]
        Resource = aws_cloudwatch_event_bus.main.arn
      },
      {
        Sid    = "SecretsRead"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        # DD API key secret included only when extension is enabled;
        # compact() removes the empty-string element when dd_enabled = false.
        Resource = compact([
          data.aws_secretsmanager_secret.slack_webhook.arn,
          var.dd_api_key_secret_arn,
        ])
      },
      {
        # T9: scoped to this account and log group prefix — not wildcard account
        Sid      = "CloudWatchLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = [
          "arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.name_prefix}-*",
          "arn:aws:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.name_prefix}-*:*",
        ]
      },
    ]
  })
}

# ── Datadog Lambda Extension layer ────────────────────────────────────────────
#
# The Datadog Extension runs as a Lambda layer alongside the function code.
# It:
#   • Receives OTLP/HTTP traces on localhost:4318 and forwards to Datadog APM
#   • Captures stdout JSON logs and ships directly to Datadog Logs
#   • Reports enhanced Lambda metrics (aws.lambda.enhanced.*) after each invocation
#   • Flushes buffered telemetry asynchronously (typically at container shutdown)
#
# Flow: Python OTel SDK → OTLP/HTTP → Extension (localhost:4318) → Datadog API (HTTPS/443)
#
# Architecture: arm64 uses the Datadog-Extension-ARM variant.
# Layer ARN pattern: arn:aws:lambda:<region>:464622532012:layer:Datadog-Extension-ARM:<version>
#
# To disable DD Extension: set var.dd_api_key_secret_arn = "" (default)

locals {
  dd_enabled              = var.dd_api_key_secret_arn != ""
  dd_extension_layer_arn  = "arn:aws:lambda:${var.region}:464622532012:layer:Datadog-Extension-ARM:${var.dd_extension_version}"

  # Common DD env vars injected into every Lambda when extension is enabled
  dd_env_vars = local.dd_enabled ? {
    DD_SITE                 = var.dd_site
    DD_SERVICE              = var.dd_service
    DD_ENV                  = var.env
    DD_VERSION              = "1.0.0"   # update via CI on deploy
    DD_LOGS_ENABLED         = "true"    # Extension captures stdout JSON → DD Logs
    DD_LOGS_INJECTION       = "true"    # Injects trace_id/span_id into log records
    DD_TRACE_ENABLED        = "true"
    # Enable HTTP OTLP receiver on the Extension (localhost:4318).
    # The Python SDK uses opentelemetry-exporter-otlp-proto-http which sends
    # HTTP/protobuf to localhost:4318.  The Extension receives locally over HTTP,
    # then forwards all telemetry to Datadog's API over HTTPS/443 externally.
    # gRPC (4317) is NOT used — omit to avoid an unnecessary open local port.
    DD_OTLP_CONFIG_RECEIVER_PROTOCOLS_HTTP_ENDPOINT = "localhost:4318"
    # Extension must explicitly enable OTLP→APM bridging or traces are dropped.
    # Without this the OTLP receiver accepts spans but never forwards them.
    DD_APM_OTLP_ENABLED                             = "true"
    # Explicit endpoint so the Python OTel SDK has no ambiguity.
    OTEL_EXPORTER_OTLP_ENDPOINT = "http://localhost:4318"
    # Datadog OTLP intake (and the Extension's forwarder) require delta temporality;
    # cumulative is dropped silently. Forced here so the SDK never defaults to cumulative.
    OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE = "delta"
    # BSP flush cadence — see _bsp_schedule_delay_ms() in observability/tracing.py
    OTEL_BSP_SCHEDULE_DELAY                         = "500"
    DD_API_KEY_SECRET_ARN   = var.dd_api_key_secret_arn   # Extension reads key at startup
    OBSERVABILITY_ENABLED   = "true"
  } : {
    OBSERVABILITY_ENABLED = "false"
  }
}

# ── Lambda — Classifier ───────────────────────────────────────────────────────

resource "aws_lambda_function" "classifier" {
  function_name    = "${local.name_prefix}-classifier"
  role             = aws_iam_role.lambda_exec.arn
  runtime          = "python3.12"
  handler          = "lambda_entrypoint.handler"
  architectures    = ["arm64"]
  memory_size      = var.lambda_memory_mb
  timeout          = var.lambda_timeout_seconds
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  # DD Extension layer — only attached when dd_api_key_secret_arn is set
  layers = local.dd_enabled ? [local.dd_extension_layer_arn] : []

  environment {
    variables = merge(
      {
        CLOUD                          = "aws"
        AWS_REGION_NAME                = var.region
        BEDROCK_MODEL_ID               = var.llm_model_id
        DYNAMODB_CLASSIFICATIONS_TABLE = aws_dynamodb_table.classifications.name
        DYNAMODB_FEEDBACK_TABLE        = aws_dynamodb_table.feedback.name
        EVENTBRIDGE_BUS_NAME           = aws_cloudwatch_event_bus.main.name
        SLACK_WEBHOOK_SECRET_NAME      = var.slack_webhook_secret_name
      },
      local.dd_env_vars,
    )
  }

  tracing_config {
    # PassThrough keeps X-Ray headers propagated but defers trace capture to
    # the Datadog Extension (OTLP) — avoids double-instrumentation cost.
    mode = local.dd_enabled ? "PassThrough" : "Active"
  }
}

resource "aws_lambda_function" "feedback" {
  function_name    = "${local.name_prefix}-feedback"
  role             = aws_iam_role.lambda_exec.arn
  runtime          = "python3.12"
  handler          = "lambda_feedback_entrypoint.handler"
  architectures    = ["arm64"]
  memory_size      = 256
  timeout          = 15
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  layers = local.dd_enabled ? [local.dd_extension_layer_arn] : []

  environment {
    variables = merge(
      {
        CLOUD                   = "aws"
        DYNAMODB_FEEDBACK_TABLE = aws_dynamodb_table.feedback.name
      },
      local.dd_env_vars,
    )
  }

  tracing_config {
    mode = local.dd_enabled ? "PassThrough" : "Active"
  }
}

# ── CloudWatch Log Groups ─────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "classifier" {
  name              = "/aws/lambda/${aws_lambda_function.classifier.function_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "feedback" {
  name              = "/aws/lambda/${aws_lambda_function.feedback.function_name}"
  retention_in_days = var.log_retention_days
}

# ── API Gateway (HTTP API) ────────────────────────────────────────────────────

resource "aws_apigatewayv2_api" "main" {
  name          = "${local.name_prefix}-api"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.main.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_apigatewayv2_integration" "classifier" {
  api_id             = aws_apigatewayv2_api.main.id
  integration_type   = "AWS_PROXY"
  integration_uri    = aws_lambda_function.classifier.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "webhook" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "POST /webhook"
  target    = "integrations/${aws_apigatewayv2_integration.classifier.id}"
}

resource "aws_apigatewayv2_integration" "feedback" {
  api_id             = aws_apigatewayv2_api.main.id
  integration_type   = "AWS_PROXY"
  integration_uri    = aws_lambda_function.feedback.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "feedback" {
  api_id    = aws_apigatewayv2_api.main.id
  route_key = "POST /feedback"
  target    = "integrations/${aws_apigatewayv2_integration.feedback.id}"
}

resource "aws_lambda_permission" "apigw_classifier" {
  statement_id  = "AllowAPIGWInvokeClassifier"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.classifier.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}

resource "aws_lambda_permission" "apigw_feedback" {
  statement_id  = "AllowAPIGWInvokeFeedback"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.feedback.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
}
