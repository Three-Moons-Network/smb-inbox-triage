# Deploying SMB Inbox Triage to AWS — Step-by-Step

> **Who this is for:** Someone with SRE experience deploying the SMB Inbox Triage stack to AWS.
> All commands are written for **Windows PowerShell**. No backslash line continuations.
> AWS account/region used throughout: **`us-east-1`**

> ### ℹ️ 2026-05-28 — Minor remediation updates
>
> The Datadog Lambda Extension works out of the box on AWS. As part of the OTel + Datadog remediation, **three small env-var additions** were made to the Lambda env (already in TF — no manual steps needed):
>
> - `DD_APM_OTLP_ENABLED=true` — extension must explicitly enable OTLP→APM bridging or traces are dropped.
> - `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta` — Datadog OTLP intake rejects cumulative; force delta everywhere.
> - `OTEL_BSP_SCHEDULE_DELAY=500` — flush BSP every 500ms instead of the OTel default 5s, since Lambda containers freeze.
>
> No re-deploy actions required if you're starting fresh; `terraform apply` puts them in place.

---

## Overview of what you're about to do

```
Phase 0  Prerequisites      Verify the three tools you need
Phase 1  AWS CLI setup      Configure credentials and verify account access
Phase 2  Bootstrap          Create the S3 state bucket + DynamoDB lock table (one-time)
Phase 3  Secrets            Store Slack webhook + Datadog API key in Secrets Manager
Phase 4  Bedrock            Verify Claude Haiku 4.5 is reachable via Bedrock
Phase 5  Build              Package the Python source
Phase 6  Terraform          Provision all AWS infrastructure
Phase 7  Test               POST a synthetic payload and watch it classify
Phase 8  Observability      Check CloudWatch logs and Datadog dashboard
Phase 9  Tear down          Destroy everything when done (optional)
```

Estimated time: **30–60 minutes**. The webhook endpoint is plain HTTPS — emails are
delivered by POSTing directly to the API Gateway URL, with no message-bus plumbing required.

---

## AWS architecture at a glance

| Concern | AWS service |
|---|---|
| Compute | Lambda (Python 3.12, arm64) |
| LLM | Amazon Bedrock Claude Haiku 4.5 |
| Datastore | DynamoDB |
| Events | EventBridge custom bus |
| HTTP endpoint | API Gateway HTTP API |
| Terraform state | S3 bucket + DynamoDB lock table |
| Observability | Datadog Lambda Extension layer (OTLP) |
| Email trigger | POST directly to the API Gateway webhook URL — no message-bus push needed |
| Auth on test requests | None — the HTTP API is open by default |

---

## Phase 0 — Prerequisites

Verify you have these three tools at the required minimums:

```powershell
aws --version       # need 2.x
terraform version   # need 1.8+
python --version    # need 3.12+
```

### Install AWS CLI v2 if missing

```powershell
winget install Amazon.AWSCLI
```

Or download the MSI from https://aws.amazon.com/cli/

---

## Phase 1 — AWS CLI Setup

### 1.1 Load credentials from .env

The `.env` file in the project root has your AWS credentials. Load them into
the current PowerShell session:

```powershell
# Run from the smb-inbox-triage project root
$env:AWS_ACCESS_KEY_ID     = "YOUR_AWS_ACCESS_KEY_ID"
$env:AWS_SECRET_ACCESS_KEY = "YOUR_AWS_SECRET_ACCESS_KEY"
$env:AWS_DEFAULT_REGION    = "us-east-1"
```

> **Persist for the session:** These env vars only last until you close the terminal.
> Re-run the block at the start of any new session, or add them to your PowerShell
> profile. Do **not** commit them or run `aws configure` with them — env vars are safer.

### 1.2 Verify the identity

```powershell
aws sts get-caller-identity
```

Expected output:

```json
{
    "UserId": "AIDAEXAMPLEID...",
    "Account": "123456789012",
    "Arn": "arn:aws:iam::123456789012:user/your-user"
}
```

If you see `InvalidClientTokenId` or `SignatureDoesNotMatch`, the credentials
in `.env` are wrong or expired — check and update them.

Save the account ID — you'll need it in a few steps:

```powershell
$ACCOUNT_ID = (aws sts get-caller-identity --query Account --output text)
echo $ACCOUNT_ID
```

### 1.3 Verify the region

```powershell
aws configure get region
```

Should print `us-east-1`. If blank:

```powershell
aws configure set region us-east-1
```

---

## Phase 2 — Bootstrap: Terraform State Backend

Terraform stores its state in an S3 bucket and uses a DynamoDB table for state
locking (prevents two `apply` runs from stepping on each other). Both already
exist — **no creation needed**. This phase just verifies they're reachable.

> **The backend `region` is simply where your state bucket lives.** It is set
> independently of the `region` you deploy resources into, and the two do not have
> to match — use whatever region holds your S3 state bucket and DynamoDB lock table.

The backend is declared in `infra/environments/aws-dev/main.tf`:

```
bucket         = "example-terraform-state"
key            = "inbox-triage/aws-dev/terraform.tfstate"
region         = "us-west-2"       # region of the state bucket + lock table
dynamodb_table = "TerraformStateLock"
```

### 2.1 Verify the state bucket is accessible

```powershell
aws s3api head-bucket --bucket example-terraform-state --region us-west-2
```

No output = bucket exists and your credentials can reach it. Any error here
means a credentials or permissions problem — fix before proceeding.

### 2.2 Verify the lock table is active

```powershell
aws dynamodb describe-table --table-name TerraformStateLock --region us-west-2 --query "Table.{Name:TableName,Key:KeySchema,Status:TableStatus}" --output table
```

Should show `ACTIVE` status and `LockID` as the hash key.

---

## Phase 3 — Secrets

Two secrets go into AWS Secrets Manager before Terraform runs:

1. **Slack webhook URL** — the classifier Lambda reads this at runtime to post notifications
2. **Datadog API key** — the Datadog Lambda Extension reads this at cold-start to authenticate

### 3.1 Create the Slack webhook secret

**Option A — you have a real Slack webhook URL:**

```powershell
aws secretsmanager create-secret `
  --name "smb-inbox-triage-slack-webhook-url" `
  --secret-string "https://hooks.slack.com/services/YOUR/REAL/URL" `
  --region us-east-1
```

**Option B — placeholder (skip Slack for now):**

```powershell
aws secretsmanager create-secret `
  --name "smb-inbox-triage-slack-webhook-url" `
  --secret-string "https://placeholder.invalid/no-slack" `
  --region us-east-1
```

### 3.2 Create the Datadog API key secret

The Datadog Lambda Extension reads the API key directly from Secrets Manager
at Lambda cold-start — no environment variable needed for the key value itself.

```powershell
aws secretsmanager create-secret `
  --name "smb-inbox-triage-dd-api-key" `
  --secret-string "YOUR_DATADOG_API_KEY" `
  --region us-east-1
```

> **If the secret already exists but is blank or wrong** (e.g. it was created
> earlier with a placeholder, or `create-secret` returns
> `ResourceExistsException`), do **not** re-run `create-secret`. Write the value
> into the existing secret with `put-secret-value` instead. This one-liner pulls
> the key straight out of `.env` at the project root so you don't paste it on the
> command line (run from the project root):
>
> ```powershell
> $ddKey = ((Get-Content .env | Where-Object { $_ -match '^\s*DD_API_KEY\s*=' }) -replace '^\s*DD_API_KEY\s*=\s*','' -replace '"','').Trim()
>
> aws secretsmanager put-secret-value `
>   --secret-id "smb-inbox-triage-dd-api-key" `
>   --secret-string $ddKey `
>   --region us-east-1
> ```
>
> If your session already loaded `.env` (via your PowerShell profile or
> `scripts/load-env.sh`), you can skip the parse and use `$env:DD_API_KEY`
> directly as the `--secret-string`.
>
> Verify it landed without printing the key (Datadog keys are 32 chars):
>
> ```powershell
> (aws secretsmanager get-secret-value --secret-id "smb-inbox-triage-dd-api-key" --query SecretString --output text --region us-east-1).Length
> ```

Save the ARN that comes back — you'll need it in Phase 6:

```powershell
$DD_SECRET_ARN = (aws secretsmanager describe-secret `
  --secret-id "smb-inbox-triage-dd-api-key" `
  --query "ARN" --output text)
echo $DD_SECRET_ARN
```

It will look like:
`arn:aws:secretsmanager:us-east-1:123456789012:secret:smb-inbox-triage-dd-api-key-xxxxxx`

### 3.3 Verify both secrets exist

```powershell
aws secretsmanager list-secrets --query "SecretList[?starts_with(Name, 'smb-inbox-triage')].Name" --output table
```

Should show both `smb-inbox-triage-slack-webhook-url` and `smb-inbox-triage-dd-api-key`.

---

## Phase 4 — Verify Bedrock Access

> **No manual activation needed.** As of 2026, the Bedrock model access page has
> been retired. Serverless foundation models are automatically enabled on first
> invocation — no console steps required.
>
> For Anthropic models specifically, first-time account users may need to submit
> use case details. If that applies to you, AWS will prompt you when you first
> invoke the model. Control over model access is now managed via IAM policies and
> Service Control Policies rather than the model access UI.

The Terraform uses model ID `us.anthropic.claude-haiku-4-5-20251001-v1:0` (defined as
the default in `infra/modules/aws/variables.tf`).

### 4.1 Smoke test Bedrock directly

> **Why `us.` prefix?** Claude Haiku 4.5 and newer models require invocation via
> a **cross-region inference profile** rather than direct on-demand throughput.
> The `us.anthropic.*` profile ID routes requests across US regions for capacity.
> Passing the bare model ID (`anthropic.claude-haiku-4-5-20251001-v1:0`) returns
> `ValidationException: on-demand throughput isn't supported`.

Run this before Terraform to confirm your credentials can reach Bedrock in `us-east-1`:

```powershell
# The AWS CLI requires --body to come from a file on Windows (inline strings
# are treated as base64 and fail). Write to a temp file first.
'{"anthropic_version":"bedrock-2023-05-31","max_tokens":10,"messages":[{"role":"user","content":"Say OK"}]}' |
  Out-File -Encoding utf8 -FilePath "$env:TEMP\bedrock-body.json"

aws bedrock-runtime invoke-model `
  --model-id "us.anthropic.claude-haiku-4-5-20251001-v1:0" `
  --body "fileb://$env:TEMP\bedrock-body.json" `
  --content-type application/json `
  --accept application/json `
  --region us-east-1 `
  "$env:TEMP\bedrock-test.json"

Get-Content "$env:TEMP\bedrock-test.json"
```

A successful response looks like:

```json
{"id":"msg_...","type":"message","role":"assistant","content":[{"type":"text","text":"OK"}],...}
```

If you get `AccessDeniedException`, the IAM user or role lacks `bedrock:InvokeModel`
on the model ARN — check the user's attached policies.

---

## Phase 5 — Build the Lambda Package

Terraform zips the `src/` directory using the `archive_file` data source, so
the build step is handled automatically at `terraform apply` time. However, if
you want to verify the source builds cleanly first:

**Git Bash / WSL:**

```bash
cd "/c/Users/linux/Desktop/cowork/consulting/practice/01-inbox-triage/smb-inbox-triage"
make build
```

**PowerShell (no make):**

```powershell
New-Item -ItemType Directory -Force -Path .build
pip install -r requirements.txt -t .build\deps\ --quiet
Set-Location .build\deps
tar -czf ..\deps.tar.gz .
Set-Location ..\..
```

After building, verify:

```powershell
Test-Path .build\lambda.zip
```

Should print `True`.

> **Note:** The AWS module uses Terraform's `archive_file` data source to zip `src/`
> on-the-fly during `terraform plan/apply`, so you don't need to build manually unless
> you want to pre-check the package size.

---

## Phase 6 — Terraform: Provision Everything

### 6.1 Set Terraform variables

Most values are in `terraform.tfvars` already. Only **secrets** need env vars (never committed):

```powershell
# 1) Datadog Lambda Extension — secret ARN from Phase 3.2 (contains account ID, never commit)
$DD_SECRET_ARN = (aws secretsmanager describe-secret `
  --secret-id "smb-inbox-triage-dd-api-key" `
  --query "ARN" --output text)
$env:TF_VAR_dd_api_key_secret_arn = $DD_SECRET_ARN

# 2) Datadog monitoring provider — raw API + APP keys that create the monitors,
#    SLO, and dashboard (now part of this same apply, no separate environment).
#    Pull from .env if it's loaded in this session, or paste the values.
$env:TF_VAR_dd_api_key = $env:DD_API_KEY   # 32-char API key
$env:TF_VAR_dd_app_key = $env:DD_APP_KEY   # application key
```

> **Datadog monitoring is now part of this environment.** The monitors, SLO, and
> dashboard are created by *this* `aws-dev` apply, gated on both keys above
> (`count = dd_api_key != "" && dd_app_key != ""`). Set both to get the full stack;
> leave `dd_api_key`/`dd_app_key` empty to deploy without Datadog monitoring (the
> Lambda Extension is still controlled separately by `dd_api_key_secret_arn`). There
> is no longer a standalone `datadog-dev` environment to apply.

> **`dd_site` is already set** in `terraform.tfvars` to `us5.datadoghq.com`.
> Do NOT set `TF_VAR_dd_site` — the tfvars value is correct and an env var would
> shadow it.  The DD Lambda Extension uses this to determine which Datadog intake
> endpoint to ship data to; a wrong site causes silent data loss (Extension ships
> to the wrong endpoint and the API key is rejected).

> **Persist the secret ARN across sessions:**
> ```powershell
> Add-Content $PROFILE '$env:TF_VAR_dd_api_key_secret_arn = (aws secretsmanager describe-secret --secret-id "smb-inbox-triage-dd-api-key" --query "ARN" --output text)'
> ```

### 6.2 Initialise Terraform

```powershell
cd infra\environments\aws-dev
terraform init
```

This downloads the AWS provider (~80 MB) and connects to the S3 state backend.
Expected ending: `Terraform has been successfully initialized!`

**Troubleshooting init failures:**

- `NoSuchBucket` — the S3 bucket name in `main.tf` doesn't match what you created.
  Update either the bucket or the `main.tf`.
- `ResourceNotFoundException` on the lock table — the DynamoDB table isn't `ACTIVE`
  yet. Wait 10 seconds and retry.
- `AccessDenied` — the IAM credentials don't have `s3:GetObject`/`s3:PutObject` on
  the bucket. Check your user permissions.

### 6.3 Review the plan

```powershell
terraform plan
```

Read the output. You should see ~25–30 AWS resources being created — plus ~5 Datadog
resources when the monitoring keys from Phase 6.1 are set. The AWS resources include:

- `aws_iam_role.lambda_exec` — execution role for both Lambdas
- `aws_iam_role_policy.lambda_policy` — scoped Bedrock, DynamoDB, EventBridge, SM access
- `aws_lambda_function.classifier` — main classifier Lambda (arm64, 512 MB)
- `aws_lambda_function.feedback` — feedback correction Lambda (arm64, 256 MB)
- `aws_cloudwatch_log_group.classifier` — 14-day log retention
- `aws_cloudwatch_log_group.feedback`
- `aws_apigatewayv2_api.main` — HTTP API (the public webhook URL)
- `aws_apigatewayv2_stage.default` — auto-deploy stage
- `aws_apigatewayv2_route.webhook` — `POST /webhook`
- `aws_apigatewayv2_route.feedback` — `POST /feedback`
- `aws_dynamodb_table.classifications` — classification log with intent GSI + TTL
- `aws_dynamodb_table.feedback` — feedback store
- `aws_cloudwatch_event_bus.main` — custom EventBridge bus
- `aws_cloudwatch_event_rule.intent_rules` × 5 — per-intent routing rules
- `aws_sqs_queue.dlq` — dead-letter queue for failed event delivery
- `aws_cloudwatch_metric_alarm.dlq_depth` — alarm if anything lands in the DLQ

Plus the Datadog monitoring resources (when `dd_api_key`/`dd_app_key` are set),
under `module.inbox_triage_datadog[0]`:

- `datadog_monitor` × 3 — error-rate, p95-latency, and human-review-rate monitors
- `datadog_service_level_objective` — availability SLO
- `datadog_dashboard` — LLM observability dashboard

> **No `destroy` actions should appear.** If you see any, stop and investigate
> before proceeding.

> **Verify observability actually got wired in.** Check both Lambda functions in
> the plan output:
>
> - `layers` should contain a `Datadog-Extension-ARM:NN` ARN
> - `OBSERVABILITY_ENABLED` should be `"true"`
> - `tracing_config { mode = "PassThrough" }` (the Extension handles tracing)
>
> If instead you see `layers = []`, `OBSERVABILITY_ENABLED = "false"`, and
> `mode = "Active"`, then `TF_VAR_dd_api_key_secret_arn` was **not set** when you
> ran `plan` — go back to Phase 6.1, set it, and re-run `terraform plan`. The
> module disables the Datadog Extension whenever that ARN is empty
> (`dd_enabled = var.dd_api_key_secret_arn != ""`), so applying without it deploys
> a working classifier that ships **no** telemetry to Datadog — and Phase 8 will
> have nothing to verify. (The Lambda Extension ships the telemetry; the Datadog
> monitors, SLO, and dashboard — created by this same apply when the monitoring keys
> are set — consume it. Phase 8 verifies both.)

### 6.4 Apply

```powershell
terraform apply
```

Type `yes` when prompted. This takes **3–8 minutes** — Lambda function creation
and API Gateway deployment are the slowest parts.

### 6.5 Save the outputs

```powershell
terraform output
```

You'll see:

```
classifier_lambda_arn = "arn:aws:lambda:us-east-1:123456789012:function:smb-inbox-triage-dev-classifier"
datastore_name        = "smb-inbox-triage-dev-classifications"
dlq_url               = "https://sqs.us-east-1.amazonaws.com/123456789012/smb-inbox-triage-dev-dlq"
event_bus_name        = "smb-inbox-triage-dev-bus"
feedback_url          = "https://xxxxxxxxxx.execute-api.us-east-1.amazonaws.com/feedback"
webhook_url           = "https://xxxxxxxxxx.execute-api.us-east-1.amazonaws.com/webhook"
```

**Save these** — you need them in Phase 7.

### 6.6 Verify the Lambdas deployed

```powershell
foreach ($fn in "smb-inbox-triage-dev-classifier","smb-inbox-triage-dev-feedback") {
  aws lambda get-function-configuration `
    --function-name $fn `
    --query "{Name:FunctionName,State:State,LastUpdate:LastUpdateStatus}" `
    --output table `
    --region us-east-1
}
```

Both `smb-inbox-triage-dev-classifier` and `smb-inbox-triage-dev-feedback` should
show `State: Active` and `LastUpdate: Successful`.

> **Why not `list-functions`?** `ListFunctions` returns only a subset of the
> function configuration and **does not populate `State`** — querying
> `Functions[].State` from it always returns `None`, which looks like a failure
> but isn't. The `State`, `LastUpdateStatus`, and `StateReason` fields are only
> returned by `GetFunction` / `GetFunctionConfiguration`, so verify per-function
> as shown above.
> See: https://docs.aws.amazon.com/lambda/latest/api/API_ListFunctions.html

---

## Phase 7 — Testing

No identity token is required — the API Gateway HTTP API is open (no IAM auth).
Just POST directly to the webhook URL.

### 7.1 Get the webhook URL

```powershell
$WEBHOOK_URL = terraform -chdir=infra\environments\aws-dev output -raw webhook_url
echo $WEBHOOK_URL
```

### 7.2 Smoke test: synthetic payload

> **Body format:** The Lambda accepts a **flat JSON object** directly. Do not wrap it
> in a message envelope such as `{"message":{"data":"..."}}`.

```powershell
$BODY = '{
  "messageId":   "test-smoke-001",
  "fromAddress": "customer@example.com",
  "fromName":    "Test Customer",
  "toAddress":   "inbox@yourbusiness.com",
  "subject":     "Where is my order?",
  "bodyText":    "Hi, I placed an order 5 days ago and have not received a shipping confirmation. Order number 12345. Please help.",
  "receivedAt":  "2026-05-25T14:30:00Z",
  "source":      "test"
}'

Invoke-RestMethod `
  -Uri "$WEBHOOK_URL" `
  -Method POST `
  -Headers @{"Content-Type"="application/json"} `
  -Body $BODY
```

A successful response:

```
status         : ok
record_id      : b4e68a32-ca7b-4e64-91d0-280d1a351381
intent         : support_request
urgency        : medium
confidence     : 0.95
requires_human : False
summary        : Customer inquiring about missing shipping confirmation for order placed 5 days ago.
```

Save the `record_id` — you'll use it to verify the DynamoDB write in the next step.

### 7.3 Test additional intent categories

Send requests with different subject lines to exercise the routing rules:

```powershell
# Helper function — builds a flat JSON email payload and POSTs it
function Send-TestEmail {
  param([string]$Subject, [string]$Body)
  $payload = @{
    messageId   = "test-$(Get-Random)"
    fromAddress = "test@example.com"
    fromName    = "Test Sender"
    toAddress   = "inbox@test.com"
    subject     = $Subject
    bodyText    = $Body
    receivedAt  = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ")
    source      = "test"
  } | ConvertTo-Json -Compress
  Invoke-RestMethod -Uri $WEBHOOK_URL -Method POST -Headers @{"Content-Type"="application/json"} -Body $payload
}

# Run all three — watch the intents in DynamoDB / CloudWatch
Send-TestEmail -Subject "Help — order #999 hasn't arrived" -Body "Three weeks and nothing. Please help."
Send-TestEmail -Subject "Interested in your pricing plans" -Body "We have 50 users and would like a demo."
Send-TestEmail -Subject "Question about my invoice" -Body "I was charged twice for last month."
```

Expected intents: `support_request`, `sales_inquiry`, `billing_question`.

### 7.4 Check DynamoDB for stored records

```powershell
# List the 5 most recent classifications (sorted by classified_at descending via GSI scan)
aws dynamodb scan `
  --table-name "smb-inbox-triage-dev-classifications" `
  --select "ALL_ATTRIBUTES" `
  --filter-expression "attribute_exists(record_id)" `
  --max-items 5 `
  --query "Items[*].{id:record_id.S,intent:intent.S,at:classified_at.S}" `
  --output table `
  --region us-east-1
```

You should see rows with `intent` values matching what you sent.

**Look up a specific record by ID** (from the smoke test response):

```powershell
aws dynamodb get-item `
  --table-name "smb-inbox-triage-dev-classifications" `
  --key "{\"record_id\":{\"S\":\"YOUR_RECORD_ID_HERE\"}}" `
  --region us-east-1
```

### 7.5 Test the feedback endpoint

The feedback Lambda verifies an HMAC-SHA256 signature on every request. To send
a valid signed correction:

```powershell
# Get the feedback URL
$FEEDBACK_URL = terraform -chdir=infra\environments\aws-dev output -raw feedback_url

# Feedback payload (replace RECORD_ID with a real one from the DynamoDB scan above)
$FEEDBACK_BODY = '{"record_id":"YOUR_RECORD_ID","corrected_intent":"sales_inquiry","reviewer":"reviewer-1"}'

# In production the caller must HMAC-sign the body with the shared secret.
# For a quick connectivity test, send unsigned — expect a 401 back (which proves
# the endpoint is alive and the signature check is working).
#
# NOTE: Windows PowerShell's Invoke-RestMethod THROWS a terminating error on any
# non-2xx status, so the expected 401 surfaces as a red WebException rather than a
# return value. Wrap it in try/catch to read the status code and body cleanly:
try {
  Invoke-RestMethod -Uri $FEEDBACK_URL -Method POST `
    -Headers @{"Content-Type"="application/json"} `
    -Body $FEEDBACK_BODY
  Write-Host "Unexpected 2xx — the signature check did NOT reject the unsigned request"
} catch {
  $code = [int]$_.Exception.Response.StatusCode
  Write-Host "HTTP $code  $($_.ErrorDetails.Message)"   # expect: HTTP 401  {"error": "Unauthorized"}
}
```

A 401 (`{"error": "Unauthorized"}`) is the **expected, correct** result — the endpoint
is live and HMAC signature enforcement is rejecting the unsigned request. If you run the
bare `Invoke-RestMethod` without the try/catch, that same 401 appears as a red
`WebCmdletWebResponseException`; it still means success. A 200 would mean the record was
corrected, which only happens when you send a valid HMAC signature.

---

## Phase 8 — Observability

### 8.1 CloudWatch logs

**Stream the classifier logs (last 50 entries):**

```powershell
aws logs tail "/aws/lambda/smb-inbox-triage-dev-classifier" `
  --since 1h `
  --format short `
  --region us-east-1
```

**Or follow in real-time while sending a test request:**

```powershell
# In one terminal — follow logs
aws logs tail "/aws/lambda/smb-inbox-triage-dev-classifier" --follow --region us-east-1

# In another terminal — send a request
Send-TestEmail -Subject "Urgent: payment failed on renewal" -Body "Please call me immediately."
```

A successful classification log line looks like:

```json
{
  "ts": "2026-05-25T14:30:01Z",
  "level": "INFO",
  "message": "Classification complete",
  "intent": "billing_question",
  "urgency": "high",
  "confidence": 0.95,
  "latency_ms": 1240,
  "cloud": "aws",
  "model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
  "dd": {
    "trace_id": "7234892348923489",
    "span_id": "3489234892"
  }
}
```

**Expected `[INFO]` "skipping" lines in dev — unconfigured optional destinations:**

When an optional destination isn't wired up, the router logs an info-level skip and
moves on — no warning, no exception, and **no APM error span**:

| Log line | Cause | Action needed |
|---|---|---|
| `Linear not configured (LINEAR_API_KEY/LINEAR_TEAM_ID unset) — skipping issue creation` | Support-request routing to Linear; keys not set | Optional — set `LINEAR_API_KEY` + `LINEAR_TEAM_ID` when you wire up Linear |
| `HubSpot not configured (HUBSPOT_API_KEY unset) — skipping contact/deal creation` | Sales-inquiry routing to HubSpot; key not set | Optional — set `HUBSPOT_API_KEY` when you wire up HubSpot |
| `Slack not configured for channel #… (no usable webhook) — skipping notification` | No per-channel webhook, and `SLACK_WEBHOOK_DEFAULT` is unset or a `.invalid` placeholder | Optional — set `SLACK_WEBHOOK_<CHANNEL>` or a real `SLACK_WEBHOOK_DEFAULT` |
| `Duplicate message_id — skipping dispatch` | Same `messageId` submitted twice; idempotency check working correctly | None — expected behaviour |

None of these affect the classification result, the DynamoDB write, the EventBridge
publish, or the HTTP 200 returned to the caller. Because each handler returns early
instead of raising, the `router.dispatch` span completes **successfully** — so these no
longer appear as `status:error` traces and do **not** count against the `error_rate`
monitor or the availability SLO provisioned by the Datadog module (now part of the
`aws-dev` environment).

> **History:** earlier builds *raised* on unconfigured destinations (`KeyError` for
> Linear/HubSpot; a missing-webhook / `ConnectError` `DestinationError` for Slack), which
> surfaced as `router.dispatch` error spans — and, for Slack pointing at a placeholder
> webhook, added a 3-retry latency penalty per dispatch. Destinations now skip gracefully
> when not configured. If you *expect* a destination to be live and still see a skip line,
> its env vars aren't set on the Lambda.

**Check the feedback Lambda logs:**

```powershell
aws logs tail "/aws/lambda/smb-inbox-triage-dev-feedback" --since 1h --format short --region us-east-1
```

### 8.2 CloudWatch metrics

Lambda publishes key metrics automatically. Check for errors and duration:

```powershell
# Pre-compute UTC timestamps — inline (Get-Date) expressions don't evaluate
# reliably when passed as AWS CLI arguments with backtick line continuation.
$start = (Get-Date).ToUniversalTime().AddHours(-1).ToString("yyyy-MM-ddTHH:mm:ssZ")
$end   = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")

# Invocation count for the last hour
aws cloudwatch get-metric-statistics `
  --namespace AWS/Lambda `
  --metric-name Invocations `
  --dimensions Name=FunctionName,Value=smb-inbox-triage-dev-classifier `
  --start-time $start --end-time $end `
  --period 3600 --statistics Sum `
  --region us-east-1

# Error count for the last hour
aws cloudwatch get-metric-statistics `
  --namespace AWS/Lambda `
  --metric-name Errors `
  --dimensions Name=FunctionName,Value=smb-inbox-triage-dev-classifier `
  --start-time $start --end-time $end `
  --period 3600 --statistics Sum `
  --region us-east-1
```

**Check the DLQ depth** (should be 0 — anything here means EventBridge routing failed):

```powershell
$DLQ_URL = terraform -chdir=infra\environments\aws-dev output -raw dlq_url
aws sqs get-queue-attributes `
  --queue-url $DLQ_URL `
  --attribute-names ApproximateNumberOfMessages `
  --region us-east-1
```

`ApproximateNumberOfMessages: 0` is what you want. A non-zero value means an
EventBridge rule failed to deliver — check the DLQ messages to see what went wrong.

### 8.3 Datadog — verify the Lambda Extension is shipping data

> **Confirmed working (2026-05-25):** Logs appear within seconds of invocation.
> APM traces appear within ~20–30 minutes of the first cold start in a new
> execution environment — the Extension buffers spans and flushes them when the
> Lambda execution environment is eventually recycled. Subsequent cold starts in
> the same environment flush faster. See the timing note below.

**Architecture:**
```
Lambda (Python)
  │  OTLP/HTTP  localhost:4318
  ▼
Datadog Lambda Extension (ARM layer)
  │  HTTPS/443
  ▼
Datadog APM + Logs (us5.datadoghq.com)
  │
  (separate path below — does not require Extension)
  │
CloudWatch Logs → Datadog Forwarder Lambda → Datadog Logs
```

The Extension receives OTLP/HTTP locally on port 4318, then forwards everything
to Datadog over HTTPS/443. There is no gRPC involved.

**Step 1 — Verify the layer is attached:**

```powershell
aws lambda get-function-configuration `
  --function-name "smb-inbox-triage-dev-classifier" `
  --query "Layers[*].Arn" `
  --region us-east-1
```

Should return an ARN containing `Datadog-Extension-ARM:97`. If empty,
`TF_VAR_dd_api_key_secret_arn` was not set when you ran `terraform apply` —
set it and re-apply.

**Step 2 — Verify OTel initialized in the Lambda logs:**

```powershell
aws logs tail "/aws/lambda/smb-inbox-triage-dev-classifier" --since 30m --format short --region us-east-1 |
  Select-String "OTel|datadog-agent"
```

Look for all four of these lines in the same cold-start stream:
```
{"DD_EXTENSION_FALLBACK_REASON":"otel"}           ← Extension is in OTLP receiver mode
TELEMETRY Name: datadog-agent State: Subscribed   ← Extension subscribed to function telemetry
OTel tracer initialised: endpoint=http://localhost:4318/v1/traces has_api_key=False
OTel trace flush succeeded
```

`has_api_key=False` is **expected** — the Python SDK sends to the local Extension,
which handles Datadog authentication using the key it fetched from Secrets Manager at
cold start (visible in CloudTrail as a `GetSecretValue` call with user-agent
`aws-sdk-go-v2 ... lang/go`).

If you see `opentelemetry packages not installed — tracing disabled`, the Lambda zip
was built before the opentelemetry packages were added to `build_lambda.py`. Trigger
a rebuild with a full `terraform apply` (not `-target`).

**Step 3 — Check Datadog APM:**

1. Open https://app.us5.datadoghq.com
2. Go to **APM → Traces**
3. Filter by `service:smb-inbox-triage` and `env:dev`

> **Timing note:** The DD Lambda Extension buffers OTLP spans during invocations
> and flushes them asynchronously — typically when the execution environment is
> eventually recycled by AWS (anywhere from minutes to hours after the last
> invocation). On a fresh deploy with a new execution environment, allow up to
> 30 minutes before the first traces appear. Subsequent invocations in a warm
> container may appear sooner once the environment has cycled at least once.
> Logs always appear within seconds via the Forwarder path.

Each trace shows the full span tree:
```
classifier.classify_email  (~1.2s total)
└── gen_ai.bedrock.converse  (~1.0s)
       gen_ai.system = "aws.bedrock"
       gen_ai.request.model = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
       gen_ai.usage.input_tokens = 847
       gen_ai.usage.output_tokens = 183
router.dispatch  (~0.1s)
```

**Step 4 — Logs in Datadog:**

Go to **Logs** → search `service:smb-inbox-triage env:dev`. Log lines have
`dd.trace_id` injected, so you can click a log line and jump directly to its trace.
Logs arrive via two paths: the Datadog CloudWatch Forwarder (seconds) and the
Extension's direct stdout capture. Both write to the same Datadog Logs index.

**Step 5 — Enhanced metrics in Datadog:**

Go to **Metrics Explorer** → search `aws.lambda.enhanced`. You should see:
- `aws.lambda.enhanced.invocations`
- `aws.lambda.enhanced.duration`
- `aws.lambda.enhanced.errors`
- `aws.lambda.enhanced.out_of_memory`

Filter by `functionname:smb-inbox-triage-dev-classifier`. These are published
by the Extension independently of OTLP. If they are absent but logs and APM
are present, the Extension has the API key but the enhanced metrics flush
hasn't occurred yet — invoke the function a few more times and wait 5 minutes.

### 8.4 Verify the Datadog monitors and dashboard

The Datadog monitoring — error-rate, latency, and human-review-rate monitors, an
availability SLO, and an LLM observability dashboard — is part of the `aws-dev`
environment and was created by the `terraform apply` in Phase 6 (when `dd_api_key`
and `dd_app_key` were set). It shares the `aws-dev` state; there is no separate
environment to apply here.

Confirm the resources exist in Datadog:

- **Dashboards** → search `smb-inbox-triage` → open the LLM Observability dashboard
- **Monitors** → search `smb-inbox-triage` → error rate, p95 latency, and human-review rate monitors

Or from Terraform state:

```powershell
terraform -chdir=infra\environments\aws-dev state list | Select-String "datadog"
```

You should see `module.inbox_triage_datadog[0].datadog_monitor.*`,
`…datadog_service_level_objective.*`, and `…datadog_dashboard.*`.

> **If nothing shows up:** `dd_api_key`/`dd_app_key` weren't set when you ran the
> Phase 6 apply, so the monitoring module was count-gated to zero. Set both
> (Phase 6.1) and re-run `terraform apply` in `aws-dev`.

### 8.5 EventBridge delivery check

Verify that EventBridge rules are receiving events:

```powershell
$start = (Get-Date).ToUniversalTime().AddHours(-1).ToString("yyyy-MM-ddTHH:mm:ssZ")
$end   = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")

# Check event counts on the custom bus
aws cloudwatch get-metric-statistics `
  --namespace AWS/Events `
  --metric-name Invocations `
  --dimensions Name=EventBusName,Value=smb-inbox-triage-dev-bus `
  --start-time $start --end-time $end `
  --period 3600 --statistics Sum `
  --region us-east-1
```

---

## Phase 9 — Teardown (when done experimenting)

### 9.1 Destroy the AWS infrastructure

```powershell
cd infra\environments\aws-dev
terraform destroy
```

Type `yes`. This destroys both Lambdas, API Gateway, DynamoDB tables, EventBridge
bus and rules, SQS DLQ, IAM role and policy, CloudWatch log groups, and — when they
were created — the Datadog monitors, SLO, and dashboard, which now live in this same
state. There's no separate Datadog teardown step.

The S3 state bucket, DynamoDB lock table, and Secrets Manager secrets are **not**
managed by Terraform — clean them up manually:

```powershell
# Delete secrets
aws secretsmanager delete-secret --secret-id "smb-inbox-triage-slack-webhook-url" --force-delete-without-recovery --region us-east-1
aws secretsmanager delete-secret --secret-id "smb-inbox-triage-dd-api-key" --force-delete-without-recovery --region us-east-1

# Empty and delete the state bucket
aws s3 rm s3://example-terraform-state --recursive --region us-west-2
aws s3api delete-bucket --bucket example-terraform-state --region us-west-2

# Delete the lock table (lives in us-west-2)
aws dynamodb delete-table --table-name TerraformStateLock --region us-west-2
```

---

## Troubleshooting

### `terraform init` fails with `NoSuchBucket`

The S3 bucket name in `infra/environments/aws-dev/main.tf` doesn't match the
bucket you created. Either rename the bucket in the file or create a bucket
with the exact name in the file.

### Lambda returns `AccessDeniedException` from Bedrock

This is the most likely issue on a fresh deploy. There are two variants:

**Variant A — wrong inference profile ARN in the IAM policy**

System-defined cross-region inference profiles (e.g. `us.anthropic.claude-haiku-4-5-20251001-v1:0`)
are scoped to your account once model access is enabled. Their actual ARN includes
your account ID:

```
arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0
```

Confirm with:

```powershell
aws bedrock list-inference-profiles --type-equals SYSTEM_DEFINED --region us-east-1 `
  --query "inferenceProfileSummaries[?contains(inferenceProfileId, 'haiku-4-5')].inferenceProfileArn"
```

The Terraform module uses `data.aws_caller_identity.current.account_id` to build
this ARN automatically — if you're seeing `AccessDeniedException` after apply,
verify the correct ARN is in the policy:

```powershell
aws iam get-role-policy `
  --role-name smb-inbox-triage-dev-lambda-role `
  --policy-name smb-inbox-triage-dev-lambda-policy `
  --query "PolicyDocument.Statement[?Sid=='BedrockInvoke'].Resource"
```

**Variant B — `terraform apply -target` used with wrong address**

The resource is inside a module. The correct target address is:

```powershell
terraform apply -target=module.inbox_triage_aws.aws_iam_role_policy.lambda_policy
```

Not `aws_iam_role_policy.lambda_policy` (missing the module prefix).

### `terraform apply` fails with `AccessDenied` on Bedrock (user credentials)

```powershell
aws iam simulate-principal-policy `
  --policy-source-arn (aws sts get-caller-identity --query Arn --output text) `
  --action-names bedrock:InvokeModel `
  --resource-arns "arn:aws:bedrock:us-east-1::foundation-model/us.anthropic.claude-haiku-4-5-20251001-v1:0" `
  --region us-east-1
```

The Lambda execution role gets `bedrock:InvokeModel` from Terraform. If Terraform
hasn't applied yet, use your own credentials temporarily to check the model is accessible.

### Lambda returns 5xx after Terraform apply

The most common cause is a missing or misconfigured environment variable on the
Lambda. Check what's actually set:

```powershell
aws lambda get-function-configuration `
  --function-name "smb-inbox-triage-dev-classifier" `
  --query "Environment.Variables" `
  --region us-east-1
```

Verify `BEDROCK_MODEL_ID`, `DYNAMODB_CLASSIFICATIONS_TABLE`, and
`SLACK_WEBHOOK_SECRET_NAME` are all present. If any are missing, a variable
in Phase 6.1 wasn't set when you ran `terraform apply` — set it and re-run apply.

### Datadog Extension: layer attached, logs/metrics in Datadog but wrong service tag, NO APM traces

**Symptom:** Logs appear in Datadog tagged `service:smb-inbox-triage-dev-classifier` (the Lambda
function name) via the existing CloudWatch Forwarder, but no enhanced Lambda metrics
(`aws.lambda.enhanced.*`) and no APM traces — even though the Extension layer is attached
and the Lambda logs show `datadog-agent State: Ready`.

**Root cause — `DD_SITE` is wrong.** The DD Lambda Extension ships enhanced metrics and APM
traces directly to the Datadog intake endpoint at `https://lambda-api.<DD_SITE>/api/v1/...`.
If `DD_SITE = "datadoghq.com"` (the module default / US1) but your account is on
`us5.datadoghq.com`, the Extension sends to the US1 endpoint with a US5 API key — the
request is rejected silently and all Extension-shipped data is lost. The CloudWatch Forwarder
uses a separate, already-configured path and is unaffected.

**Verify:**

```powershell
aws lambda get-function-configuration `
  --function-name "smb-inbox-triage-dev-classifier" `
  --query "Environment.Variables.DD_SITE" `
  --region us-east-1
```

Should print `us5.datadoghq.com`. If it prints `datadoghq.com`, `dd_site` was not set.

**Fix:** `dd_site = "us5.datadoghq.com"` is now set in `terraform.tfvars` — a full
`terraform apply` will push the correct value to the Lambda env vars.

---

### Datadog Extension not shipping traces

> **Verified diagnostic:** If `Billed Duration ≈ Duration` (zero post-END overhead)
> across multiple warm invocations, the Extension is making no outbound network calls.
> A healthy Extension flushing spans to Datadog adds 50–200ms to Billed Duration
> after the END line. Zero overhead = Extension is either not authenticated or not
> flushing (see failure modes below). Confirm via CloudTrail that the Extension
> (`GetSecretValue` with user-agent `aws-sdk-go-v2 ... lang/go`) is fetching the
> API key secret successfully.

There are three distinct failure modes. Check them in order.

**1 — Layer not attached (most common on first deploy)**

```powershell
aws lambda get-function-configuration `
  --function-name "smb-inbox-triage-dev-classifier" `
  --query "Layers[*].Arn" `
  --region us-east-1
```

If the list is empty, `TF_VAR_dd_api_key_secret_arn` was not set when you ran
`terraform apply`. The module defaults to `dd_enabled = false` when the ARN is
empty, which skips the layer AND sets `OBSERVABILITY_ENABLED = "false"`:

```powershell
# Set the ARN and re-apply
$DD_SECRET_ARN = (aws secretsmanager describe-secret `
  --secret-id "smb-inbox-triage-dd-api-key" `
  --query "ARN" --output text)
$env:TF_VAR_dd_api_key_secret_arn = $DD_SECRET_ARN

cd infra\environments\aws-dev
terraform apply
```

**2 — opentelemetry packages missing from the Lambda zip**

The DD Extension receives OTLP from the Python code — but if opentelemetry
packages aren't in the zip, the Python SDK silently disables itself. Check the
Lambda logs:

```powershell
aws logs tail "/aws/lambda/smb-inbox-triage-dev-classifier" `
  --since 30m --format short --region us-east-1 | Select-String "opentelemetry"
```

If you see `opentelemetry packages not installed — tracing disabled`, the zip was
built before `build_lambda.py` included the OTel packages. Trigger a full rebuild:

```powershell
# From the project root — delete the cached build, then re-apply
Remove-Item -Recurse -Force .build
cd infra\environments\aws-dev
terraform apply
```

`build_lambda.py` now installs `opentelemetry-sdk` and
`opentelemetry-exporter-otlp-proto-http` with the arm64 manylinux wheel, alongside
pydantic and httpx.

**3 — OTLP endpoint/protocol mismatch**

The Python SDK uses the HTTP exporter (port 4318). The DD Lambda Extension is
configured to receive on `localhost:4318` via
`DD_OTLP_CONFIG_RECEIVER_PROTOCOLS_HTTP_ENDPOINT`. The Extension then forwards to
Datadog's API over HTTPS/443 — there is no gRPC in this path.

If traces are missing but the layer is attached and OTel shows as initialised,
verify the Lambda environment variables include both the OTLP HTTP receiver config
and the explicit endpoint:

```powershell
aws lambda get-function-configuration `
  --function-name "smb-inbox-triage-dev-classifier" `
  --query "Environment.Variables.{OTLP:DD_OTLP_CONFIG_RECEIVER_PROTOCOLS_HTTP_ENDPOINT,Endpoint:OTEL_EXPORTER_OTLP_ENDPOINT,Obs:OBSERVABILITY_ENABLED}" `
  --region us-east-1
```

Expected values:
- `DD_OTLP_CONFIG_RECEIVER_PROTOCOLS_HTTP_ENDPOINT` = `localhost:4318`
- `OTEL_EXPORTER_OTLP_ENDPOINT` = `http://localhost:4318`
- `OBSERVABILITY_ENABLED` = `true`

If these are missing, the Terraform has an older state — run `terraform apply` to
push the updated env vars.

Also verify the Extension startup in logs:

```powershell
aws logs tail "/aws/lambda/smb-inbox-triage-dev-classifier" `
  --since 30m --format short --region us-east-1 | Select-String "datadog|OTel"
```

### DLQ has messages — routing failed

Pull the message to see what went wrong:

```powershell
$DLQ_URL = terraform -chdir=infra\environments\aws-dev output -raw dlq_url
aws sqs receive-message --queue-url $DLQ_URL --region us-east-1
```

The message body will contain the original event and the failure reason. Common
causes: the EventBridge rule pattern didn't match the actual `detail.intent`
value (check spelling), or the target (if you've added one) returned an error.

### Lambda returns `classified_not_stored` — classification succeeded but DynamoDB write failed

Check the logs for the actual cause:

```powershell
aws logs filter-log-events `
  --log-group-name /aws/lambda/smb-inbox-triage-dev-classifier `
  --start-time ([DateTimeOffset]::UtcNow.AddMinutes(-10).ToUnixTimeMilliseconds()) `
  --filter-pattern "DynamoDB" `
  --region us-east-1 `
  --query "events[*].message" --output text
```

**If you see `Float types are not supported. Use Decimal types instead.`** — this
means `build_lambda.py` ran before the `parse_float=Decimal` fix was in place.
The fix is already in `lambda_entrypoint.py`; a full `terraform apply` (not
`-target`) will rebuild the zip and update the function.

### `build_lambda.py` fails with `PermissionError: [WinError 5]` on `__pycache__`

Windows marks `__pycache__` directories read-only after Python writes `.pyc` files.
The `shutil.rmtree` call in `build_lambda.py` now includes a `_force_remove` handler
that clears the bit and retries — this is already in the script. If you see this
error on an older checkout, pull the latest `scripts/build_lambda.py`.

If it still fails (e.g. another process is holding a handle to the file), manually
delete the directory and retry:

```powershell
Remove-Item -Recurse -Force .build\package
terraform apply
```

### Cold start latency is high (> 5 seconds)

Lambda arm64 cold starts are typically 1–2 seconds for this package size. If
you're seeing higher, the Datadog Extension initialization is the most likely
cause. To measure extension overhead:

```powershell
$start = (Get-Date).ToUniversalTime().AddHours(-1).ToString("yyyy-MM-ddTHH:mm:ssZ")
$end   = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")

aws cloudwatch get-metric-statistics `
  --namespace AWS/Lambda `
  --metric-name InitDuration `
  --dimensions Name=FunctionName,Value=smb-inbox-triage-dev-classifier `
  --start-time $start --end-time $end `
  --period 3600 --statistics Average --region us-east-1
```

Init duration above 3 seconds typically indicates a slow secrets fetch at cold-start.
This is a one-time cost per container instance — warm invocations are unaffected.

### Bedrock returns `ThrottlingException`

Claude Haiku 4.5 has a default on-demand quota of ~50 RPM on new accounts.
For this dev workload that's irrelevant, but if you're running batch evals:

```powershell
aws service-quotas get-service-quota `
  --service-code bedrock `
  --quota-code L-...  # find the exact code in the Service Quotas console
  --region us-east-1
```

Or check limits in the console: https://us-east-1.console.aws.amazon.com/servicequotas/home/services/bedrock/quotas

---

## Cost Reference (low volume, < 1,000 emails/month)

| Service | Estimated monthly cost |
|---------|----------------------|
| Lambda (classifier + feedback) | $0 — free tier: 1M requests + 400K GB-s |
| Amazon Bedrock (Claude Haiku 4.5) | ~$0.04 per 1,000 emails (input + output tokens) |
| DynamoDB | $0 — free tier: 25 GB storage, 25 WCU/RCU |
| API Gateway HTTP API | $0 — free tier: 1M requests/month for 12 months |
| EventBridge | $0 — free tier: 14M events/month |
| SQS (DLQ) | $0 — free tier: 1M requests/month |
| Secrets Manager | ~$0.40/month (2 secrets × $0.40 each) |
| CloudWatch Logs | ~$0.03 (14-day retention, low volume) |
| S3 (state bucket) | < $0.01 |
| **Total** | **~$0.50/month** |

> **Bedrock pricing note:** Claude Haiku 4.5 is priced at $0.08/MTok input and
> $0.40/MTok output (on-demand, us-east-1 as of May 2026). A typical email
> classification uses ~900 input tokens and ~200 output tokens.

---

## What's Next

1. **Run the eval harness** — from Git Bash: `make eval-bedrock` to benchmark
   Claude Haiku 4.5 against the golden dataset
2. **Benchmark across providers** — `make eval-compare` to compare model accuracy
   and cost across whichever provider adapters you have configured
3. **Connect a real email source** — point your mail provider at the API Gateway
   webhook URL: e.g. an SES receiving rule that invokes the Lambda, or your inbox
   provider's push/webhook POSTing the flat JSON payload to the webhook URL
4. **Add Bedrock Guardrails** — apply content filtering if email content is
   sensitive; add via `aws_bedrock_guardrail` resource in the Terraform module
5. **Provision the Datadog SLO** — the `datadog-dev` environment creates an
   availability SLO and latency monitors; wire up a PagerDuty integration by
   setting `TF_VAR_notification_pagerduty` before applying

---

*Document updated: 2026-05-25 — smoke-tested end-to-end | Region: us-east-1 | Model: us.anthropic.claude-haiku-4-5-20251001-v1: