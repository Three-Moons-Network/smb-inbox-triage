# SMB Inbox Triage & Routing

AI-powered email classifier and router built three times — AWS, Azure, GCP — using the same Python core and a single Terraform repo. 

## What it does

Inbound emails (Gmail / Microsoft 365) are classified by intent, structured fields are extracted, and the result is routed to the right downstream system automatically:

| Intent | Destination |
|---|---|
| `sales_inquiry` | HubSpot deal + Slack #sales |
| `support_request` | Linear issue + Slack #support |
| `billing_question` | Slack #billing (DM owner if urgent) |
| `urgent_escalation` | Slack #incidents + owner DM |
| `marketing_noise` | Archive (no notification) |
| `unknown` / low confidence | Human review queue |

A feedback webhook lets humans correct misclassifications — corrections are stored and feed back into the eval dataset.

## Architecture

```
Email source (Gmail Pub/Sub / Graph API webhook)
        │
        ▼
  HTTP endpoint  ──►  Classifier function  ──►  LLM (Bedrock / Azure OpenAI / Vertex AI)
                              │
                              ▼
                    Event bus (EventBridge / Logic Apps / Pub/Sub)
                              │
               ┌──────────────┼──────────────┐
               ▼              ▼              ▼
            HubSpot         Linear         Slack
```

## Cloud service mapping

| Layer | AWS | Azure | GCP |
|---|---|---|---|
| Compute | Lambda (Python 3.12, arm64) | Azure Functions v4 (Flex) | Cloud Functions 2nd gen |
| LLM | Bedrock (Claude 3 Haiku) | Azure OpenAI (GPT-4o-mini) | Vertex AI (Gemini 1.5 Flash) |
| Event bus | EventBridge | Logic Apps Standard | Pub/Sub + Eventarc |
| Datastore | DynamoDB | Cosmos DB (Serverless) | Firestore Native |
| Secrets | Secrets Manager | Key Vault | Secret Manager |

## Prerequisites

- Python 3.12
- Terraform >= 1.8
- Cloud CLI credentials:
  - AWS: `aws configure` or `AWS_PROFILE`
  - Azure: `az login`
  - GCP: `gcloud auth application-default login`

## Quickstart

### 1. Install Python dependencies

```bash
make install
```

### 2. Run unit tests + eval gate (no cloud credentials needed)

```bash
make test
make eval
```

### 3. Build the deployment package

```bash
make build
# outputs .build/lambda.zip and .build/function_source.zip
```

### 4. Deploy to a cloud

Set required environment variables first (see below), then:

```bash
# AWS
make deploy-aws

# Azure
make deploy-azure

# GCP
make deploy-gcp
```

After deploy, Terraform outputs the webhook URL. Configure your email provider to push to it.

## Required environment variables per cloud

### AWS
```
AWS_PROFILE or AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY
TF_VAR_slack_webhook_secret_name="smb-inbox-triage/slack-webhook-url"
```
Create the Slack webhook URL secret in Secrets Manager before deploying.

### Azure
```
ARM_SUBSCRIPTION_ID
ARM_TENANT_ID
ARM_CLIENT_ID
ARM_CLIENT_SECRET
TF_VAR_azure_openai_endpoint="https://your-resource.openai.azure.com/"
```
Create Key Vault secrets `azure-openai-key`, `slack-webhook-url`, and `cosmos-connection-string` after the Key Vault is provisioned.

### GCP
```
GOOGLE_CREDENTIALS or GOOGLE_APPLICATION_CREDENTIALS
TF_VAR_gcp_project_id="your-project-id"
```

## Running live evals

After deploying, test accuracy against real LLMs:

```bash
make eval-bedrock      # Claude 3 Haiku via Bedrock
make eval-azure        # GPT-4o-mini via Azure OpenAI
make eval-gcp          # Gemini 1.5 Flash via Vertex AI
make eval-compare      # All three side-by-side comparison
```

## Feedback loop

POST corrections to the feedback endpoint:

```bash
curl -X POST https://<your-feedback-url>/feedback \
  -H "Content-Type: application/json" \
  -d '{
    "record_id": "<uuid from classification response>",
    "corrected_intent": "support_request",
    "reviewer": "your@email.com"
  }'
```

## Project structure

```
smb-inbox-triage/
├── infra/               # Terraform (modules + environments)
├── src/                 # Python runtime (cloud-agnostic core)
│   ├── classifier/      # handler, models, prompts
│   ├── adapters/        # Bedrock / Azure OpenAI / Vertex AI
│   ├── router/          # rule engine + destinations
│   └── feedback/        # correction webhook + store
├── evals/               # Golden dataset + eval harness
└── tests/               # unit / integration / e2e
```

## License

Released under the [MIT License](LICENSE.md) — free to use, modify, and extend, including commercially. No proprietary code. Copyright (c) 2026 Three Moons Network.
