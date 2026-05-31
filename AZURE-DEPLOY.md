# Deploying SMB Inbox Triage to Azure — Step-by-Step

> **Who this is for:** Someone with SRE experience who hasn't touched their Azure account in a while.
> All commands are written for **Windows PowerShell**. No backslash line continuations.
> Azure region used throughout: **`eastus`**

> ### ⚠️ ARCHITECTURE CHANGE — 2026-05-28
>
> Azure deploy migrated from **Azure Functions Flex Consumption** → **Azure Container Apps with a Datadog Agent sidecar**. The reason: Flex Consumption has no sidecar/extension story for Datadog, so OTLP telemetry never landed at Datadog. Container Apps lets us run a real DD Agent sidecar absorbing OTLP on `localhost:4318`.
>
> **What changed in this doc:**
> - Phase 5 now provisions Container Apps + ACR + UAMI, not Function Apps.
> - **New Phase 5.5** — build & push container image to ACR (no more `func azure functionapp publish` or zip deploy).
> - Phase 6 (Key Vault secrets) shrinks: `dd-api-key` and `azure-openai-key` are now passed via `TF_VAR_*` env vars and stored directly as Container App secret values, because Container Apps does NOT resolve the `@Microsoft.KeyVault(SecretUri=...)` syntax the way Function Apps did. KV is still useful for storing the source-of-truth values.
> - Phase 7 (Cosmos RBAC) is fully automated by Terraform now via the shared UAMI.
> - Phase 9 (Observability) — Datadog flow is via Agent sidecar, not direct OTLP intake.
> - **Known gotchas baked into the TF**: `function_app.py` shim selects classifier vs feedback via `FUNCTION_TARGET`; `PYTHON_ENABLE_OPENTELEMETRY=false` because our SDK setup doesn't register a global propagator; `FUNCTIONS_HTTPWORKER_PORT` must NOT be set (forces "custom handler" mode and breaks discovery); `AzureWebJobsStorage`, `FUNCTIONS_WORKER_RUNTIME=python`, `FUNCTIONS_EXTENSION_VERSION=~4` must all be set on the app container.
>
> The pre-migration Flex Consumption flow is preserved at the **bottom of this doc** under "Appendix: Pre-2026-05-28 Flex Consumption flow" if you ever need to roll back.

---

## Overview of what you're about to do

```
Phase 0  Prerequisites        Verify the four tools you need (incl. Docker Desktop)
Phase 1  Azure CLI setup      Log in, set subscription, capture IDs
Phase 2  Resource providers   Register Azure services (+ Microsoft.App for Container Apps)
Phase 3  Bootstrap            Create the Azure Blob Storage Terraform state backend (one-time)
Phase 4  Azure OpenAI         Create the resource, deploy gpt-4.1-mini, smoke-test it
Phase 5  Terraform            Pass 1: registry → Phase 5.5 push image → Pass 2: the rest
Phase 5.5 Build & push image  docker build, az acr login, docker push (between the two passes)
Phase 6  Populate secrets     Store source-of-truth secrets in Key Vault (optional)
Phase 7  Cosmos RBAC          Automatic via Terraform UAMI — verify only
Phase 8  Test                 POST a synthetic payload and watch it classify
Phase 9  Observability        Datadog logs + traces via Agent sidecar
Phase 10 Tear down            Destroy everything when done (optional)
```

Estimated time: **60–90 minutes** (Docker build + Azure OpenAI model propagation are the slowest steps).

---

## The Azure stack at a glance

| Concern | Implementation |
|---|---|
| Compute | Azure Container Apps (multi-container with DD Agent sidecar) |
| LLM | Azure OpenAI GPT-4.1-mini |
| Datastore | Cosmos DB (NoSQL API, Serverless) |
| Events | Logic Apps Standard |
| HTTP endpoint | Container Apps ingress |
| Image registry | ACR (Azure Container Registry) |
| Terraform state | Azure Blob Storage container |
| Secrets | Key Vault (source of truth) + Container App secrets (runtime) |
| Managed auth | User-Assigned Managed Identity (shared by both apps) |
| Observability | OTLP → DD Agent sidecar → Datadog |
| Test auth | No token — Container App ingress is public |

---

## Phase 0 — Prerequisites

This deploy needs four CLI tools: **Terraform**, **Python**, **Azure CLI**, and **Docker Desktop**. Verify your versions:

```powershell
az version        # need 2.50+ (any 2.x is fine)
terraform version # need 1.8+
python --version  # need 3.12+
docker --version  # need 20.10+ (Docker Desktop on Windows)
```

### Install or update Azure CLI

```powershell
winget install Microsoft.AzureCLI
```

Or download the MSI from https://learn.microsoft.com/en-us/cli/azure/install-azure-cli-windows

After installing, open a **new** PowerShell window so `az` is on the PATH, then verify:

```powershell
az version
```

### Install Docker Desktop (if missing)

```powershell
winget install Docker.DockerDesktop
```

Launch Docker Desktop once and sign in (free tier is fine). Confirm:

```powershell
docker run --rm hello-world
```

---

## Phase 1 — Azure CLI Setup

### 1.1 Log in

Use `--use-device-code` to avoid the localhost browser-callback issue on Windows.
This prints a URL and a code — open the URL in your browser, enter the code, sign in.

```powershell
az login --use-device-code
```

After signing in, the CLI prints a list of subscriptions. If you only have one, it's
already selected. If you have multiple:

```powershell
az account list --output table
```

Set the one you want to use:

```powershell
az account set --subscription "YOUR_SUBSCRIPTION_NAME_OR_ID"
```

### 1.2 Capture the IDs you'll need throughout this guide

```powershell
$SUBSCRIPTION_ID = (az account show --query id --output tsv)
$TENANT_ID       = (az account show --query tenantId --output tsv)
echo "Subscription: $SUBSCRIPTION_ID"
echo "Tenant:       $TENANT_ID"
```

Save these. You'll set them as `ARM_*` environment variables in a moment.

### 1.3 Set ARM environment variables for Terraform

The `azurerm` Terraform provider reads these automatically.
Run this at the start of every new session before running any Terraform commands:

```powershell
$env:ARM_SUBSCRIPTION_ID = $SUBSCRIPTION_ID
$env:ARM_TENANT_ID       = $TENANT_ID
```

> **Persist for every session:** Add these two lines to your PowerShell profile:
> ```powershell
> Add-Content $PROFILE '$env:ARM_SUBSCRIPTION_ID = (az account show --query id --output tsv)'
> Add-Content $PROFILE '$env:ARM_TENANT_ID       = (az account show --query tenantId --output tsv)'
> ```

### 1.4 Verify the identity

```powershell
az account show
```

Expected output:

```json
{
  "id":             "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "name":           "Your Subscription Name",
  "state":          "Enabled",
  "tenantId":       "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
  "user": {
    "name":         "you@example.com",
    "type":         "user"
  }
}
```

---

## Phase 2 — Register Resource Providers

Azure requires explicit registration before using most services. If your subscription
hasn't used these before (or hasn't in a long time), they may be in `NotRegistered`
state and Terraform will fail with a generic `AuthorizationFailed` error.

Register everything needed for this project in one shot:

```powershell
az provider register --namespace Microsoft.App            # Container Apps (NEW — required since 2026-05-28 migration)
az provider register --namespace Microsoft.ContainerRegistry  # ACR
az provider register --namespace Microsoft.Web            # Logic Apps host
az provider register --namespace Microsoft.CognitiveServices  # Azure OpenAI
az provider register --namespace Microsoft.DocumentDB     # Cosmos DB
az provider register --namespace Microsoft.KeyVault       # Key Vault
az provider register --namespace Microsoft.Logic          # Logic Apps
az provider register --namespace Microsoft.Storage        # Storage Accounts
az provider register --namespace Microsoft.Insights       # Application Insights
az provider register --namespace Microsoft.OperationalInsights  # Log Analytics
az provider register --namespace Microsoft.ManagedIdentity  # User-Assigned Managed Identity
```

> **Note:** `Microsoft.App` is the most likely to be in `NotRegistered` state if you've never used Container Apps. Terraform will return a `409 MissingSubscriptionRegistration` error if it tries to create a `azurerm_container_app_environment` resource against an unregistered subscription. If that happens, run `az provider register --namespace Microsoft.App --wait` and retry the apply.

Registration is asynchronous — it typically takes 30–60 seconds. Check when done:

```powershell
$namespaces = @(
  "Microsoft.App", "Microsoft.ContainerRegistry", "Microsoft.ManagedIdentity",
  "Microsoft.Web", "Microsoft.CognitiveServices", "Microsoft.DocumentDB",
  "Microsoft.KeyVault", "Microsoft.Logic", "Microsoft.Storage",
  "Microsoft.Insights", "Microsoft.OperationalInsights"
)
$namespaces | ForEach-Object {
  $state = (az provider show --namespace $_ --query "registrationState" --output tsv)
  "{0,-40} {1}" -f $_, $state
}
```

All should show `Registered`. Re-run this check until they do — some take a full minute.

---

## Phase 3 — Bootstrap: Terraform State Backend

Terraform stores its state in an Azure Blob Storage container. The storage account and
container must exist **before** you run `terraform init`. This is a one-time manual step.

The backend is declared in `infra/environments/azure-dev/main.tf`:

```
resource_group_name  = "rg-tfstate"
storage_account_name = "tfstateinboxtriage"
container_name       = "tfstate"
key                  = "inbox-triage/azure-dev/terraform.tfstate"
```

### 3.1 Create the resource group for Terraform state

```powershell
az group create --name "rg-tfstate" --location "eastus"
```

### 3.2 Create the storage account

> **Storage account names must be globally unique** across all of Azure. The name
> `tfstateinboxtriage` is already hardcoded in the Terraform backend block. If this
> name is taken (unlikely since it's your initials), update the name in
> `infra/environments/azure-dev/main.tf` to match whatever you create here.

```powershell
az storage account create `
  --name "tfstateinboxtriage" `
  --resource-group "rg-tfstate" `
  --location "eastus" `
  --sku "Standard_LRS" `
  --kind "StorageV2" `
  --min-tls-version TLS1_2 `
  --allow-blob-public-access false
```

> **Why `--min-tls-version TLS1_2`:** the `az storage account create` CLI still
> defaults new accounts to `TLS1_0`. Set it explicitly here so the state backend
> isn't created with a weak floor. (If you bootstrapped before this line was added,
> fix the existing account with
> `az storage account update --name tfstateinboxtriage --resource-group rg-tfstate --min-tls-version TLS1_2`.)

### 3.3 Create the blob container

```powershell
az storage container create `
  --name "tfstate" `
  --account-name "tfstateinboxtriage" `
  --auth-mode login
```

### 3.4 Get the storage access key for the Terraform backend

The `azurerm` Terraform backend authenticates to Blob Storage using the storage
account access key (see security decision T21 — OIDC is deferred until CI/CD is set up).

```powershell
$ARM_ACCESS_KEY = (az storage account keys list `
  --resource-group "rg-tfstate" `
  --account-name "tfstateinboxtriage" `
  --query "[0].value" --output tsv)
$env:ARM_ACCESS_KEY = $ARM_ACCESS_KEY
echo "ARM_ACCESS_KEY set (length: $($ARM_ACCESS_KEY.Length))"
```

> **Persist the access key:** This key doesn't change unless you rotate it.
> Add it to your profile or store it in your `.env` file:
> ```powershell
> Add-Content $PROFILE ('$env:ARM_ACCESS_KEY = "' + $ARM_ACCESS_KEY + '"')
> ```
> Treat this like a password — do not commit it.

### 3.5 Verify the backend is reachable

```powershell
az storage container show `
  --name "tfstate" `
  --account-name "tfstateinboxtriage" `
  --auth-mode login `
  --query "name" --output tsv
```

Should print `tfstate`. Any error here means a credentials or permissions problem.

---

## Phase 4 — Azure OpenAI Setup

### 4.1 Check if an Azure OpenAI resource already exists

```powershell
az cognitiveservices account list `
  --query "[?kind=='OpenAI'].{Name:name, Location:location, RG:resourceGroup}" `
  --output table
```

If you see a resource, great — skip to 4.3 to get its endpoint and API key.
If the list is empty, create one now.

### 4.2 Create an Azure OpenAI resource (if needed)

> **Approval required for new accounts:** Azure OpenAI requires an approved subscription.
> Most subscriptions are auto-approved now, but if you get `SubscriptionNotRegistered`
> or a capacity error, you may need to request access at
> https://aka.ms/oai/access and wait 1–2 business days.

```powershell
az cognitiveservices account create `
  --name "inbox-triage-openai" `
  --resource-group "rg-tfstate" `
  --kind "OpenAI" `
  --sku "S0" `
  --location "eastus" `
  --yes
```

Wait for `provisioningState: "Succeeded"` before continuing:

```powershell
az cognitiveservices account show `
  --name "inbox-triage-openai" `
  --resource-group "rg-tfstate" `
  --query "properties.provisioningState" --output tsv
```

### 4.3 Deploy the gpt-4.1-mini model

The deployment name must match `llm_model_id` in `terraform.tfvars` (currently `gpt-4.1-mini`).

> **Why not gpt-4o-mini?** Version `2024-07-18` was deprecated March 2026 and Azure blocks
> new deployments of it. `gpt-4.1-mini` (2025-04-14) is the direct successor —
> same small/fast/cheap positioning, `GenerallyAvailable` as of May 2026.
>
> **Why Standard SKU?** GlobalStandard quota starts at 0 on accounts that haven't
> previously deployed Azure OpenAI. Standard (regional) has 200K TPM available by
> default and is identical for dev workloads.

```powershell
az cognitiveservices account deployment create `
  --name "inbox-triage-openai" `
  --resource-group "rg-tfstate" `
  --deployment-name "gpt-4.1-mini" `
  --model-name "gpt-4.1-mini" `
  --model-version "2025-04-14" `
  --model-format "OpenAI" `
  --sku-name "Standard" `
  --sku-capacity 30
```

Deployment takes **5–10 minutes**. Wait until it shows `Succeeded`:

```powershell
az cognitiveservices account deployment show `
  --name "inbox-triage-openai" `
  --resource-group "rg-tfstate" `
  --deployment-name "gpt-4.1-mini" `
  --query "properties.provisioningState" --output tsv
```

### 4.4 Get the endpoint and API key

```powershell
$AZURE_OPENAI_ENDPOINT = (az cognitiveservices account show `
  --name "inbox-triage-openai" `
  --resource-group "rg-tfstate" `
  --query "properties.endpoint" --output tsv)

$AZURE_OPENAI_KEY = (az cognitiveservices account keys list `
  --name "inbox-triage-openai" `
  --resource-group "rg-tfstate" `
  --query "key1" --output tsv)

echo "Endpoint: $AZURE_OPENAI_ENDPOINT"
echo "Key length: $($AZURE_OPENAI_KEY.Length)"
```

### 4.5 Smoke test the deployment

```powershell
$body = @{
  messages = @(@{ role = "user"; content = "Say OK" })
  max_tokens = 10
} | ConvertTo-Json -Compress

$response = Invoke-RestMethod `
  -Uri "${AZURE_OPENAI_ENDPOINT}openai/deployments/gpt-4.1-mini/chat/completions?api-version=2024-08-01-preview" `
  -Method POST `
  -Headers @{ "api-key" = $AZURE_OPENAI_KEY; "Content-Type" = "application/json" } `
  -Body $body

$response.choices[0].message.content
```

Should print `OK` (or similar). A `404` means the deployment name is wrong or hasn't
propagated yet — wait 2 more minutes and retry.

---

## Phase 5 — Terraform: Provision Everything

### 5.1 Set Terraform variables

Container Apps + DD Agent sidecar needs **three** sensitive values passed via `TF_VAR_*` env vars because Container Apps cannot resolve Key Vault `@Microsoft.KeyVault(...)` references:

```powershell
# Required — Azure OpenAI endpoint (contains account name, never commit)
$env:TF_VAR_azure_openai_endpoint = $AZURE_OPENAI_ENDPOINT

# Required — Azure OpenAI API key, literal value
$env:TF_VAR_azure_openai_api_key = $AZURE_OPENAI_KEY

# Required — Datadog API key, literal value (your real DD key). Double duty: the
# DD Agent sidecar's secret AND the datadog provider's api_key.
$env:TF_VAR_dd_api_key = $env:DD_API_KEY   # 32-char API key, loaded from your .env via profile
```

The Datadog **monitoring** (monitors, SLO, dashboard) stands up with this same `azure-dev` apply — there is no standalone `datadog-dev` environment and no cross-environment state. It is gated on the Datadog *application* key, so set that too if you want the monitors created:

```powershell
# Datadog application key — creates the monitors/SLO/dashboard. Optional.
$env:TF_VAR_dd_app_key = $env:DD_APP_KEY   # application key
```

> **Datadog monitoring is part of this environment.** The monitors, SLO, and
> dashboard are created by *this* `azure-dev` apply, gated on both Datadog keys
> (`count = dd_api_key != "" && dd_app_key != ""`). Set both to get the full stack;
> leave `dd_app_key` empty to deploy the app (with its sidecar) but skip the Datadog
> monitors. The monitoring lives in `azure-dev`'s own state — there is no separate
> environment to apply and no cross-account/cross-environment backend.

> **`dd_site` is already set** in `terraform.tfvars` to `us5.datadoghq.com` — do NOT
> set `TF_VAR_dd_site`; an env var would shadow the (correct) tfvars value. It selects
> the Datadog intake/API endpoint for both the sidecar and the monitoring provider.

Verify the values are set:

```powershell
@("TF_VAR_azure_openai_endpoint","TF_VAR_azure_openai_api_key","TF_VAR_dd_api_key","TF_VAR_dd_app_key") | ForEach-Object {
  $v = [System.Environment]::GetEnvironmentVariable($_)
  if ($v) { "{0,-32} OK ({1} chars)" -f $_, $v.Length } else { "{0,-32} MISSING" -f $_ }
}
```

The first three must show OK. `TF_VAR_dd_app_key` may show MISSING — that only skips the Datadog monitors/SLO/dashboard; the app and its sidecar still deploy.

> **Persist across sessions:** see `scripts/setup-ps-profile.ps1` — it loads everything from `.env` automatically on every PowerShell start.

### 5.2 Initialise Terraform

```powershell
cd infra\environments\azure-dev
terraform init
```

This downloads the Azure provider (~100 MB) and connects to the Blob Storage backend.
Expected ending: `Terraform has been successfully initialized!`

**Troubleshooting init failures:**

- `Error: Failed to get existing workspaces` or `AuthorizationFailed` — `ARM_ACCESS_KEY`
  is not set. Set it (Phase 3.4) and retry.
- `container not found` — the blob container name in `main.tf` doesn't match what you
  created in Phase 3.3. Update one to match.
- `ResourceGroupNotFound` — the resource group for the state doesn't exist yet. Run
  Phase 3.1 first.

### 5.3 Review the plan

```powershell
terraform plan -out=tfplan
```

Read the output. You should see ~25–30 resources being created including:

- `azurerm_resource_group.main` — `rg-smb-inbox-triage-dev` in eastus
- `azurerm_storage_account.main` — shared storage; also hosts `AzureWebJobsStorage` for the Functions host
- `azurerm_container_registry.main` — **`acrinboxtriagedev`** (ACR Basic SKU, admin disabled)
- `azurerm_user_assigned_identity.app` — **`uami-smb-inbox-triage-dev`** (shared by both Container Apps)
- `azurerm_role_assignment.uami_acr_pull` — UAMI → AcrPull on the registry
- `azurerm_key_vault.main` — `your-key-vault-name`
- `azurerm_key_vault_access_policy.uami_secrets` — UAMI → Get on KV secrets
- `azurerm_application_insights.main`
- `azurerm_log_analytics_workspace.main` — required by Container App Environment
- `azurerm_container_app_environment.main` — `cae-smb-inbox-triage-dev`
- `azurerm_container_app.classifier` — `ca-smb-inbox-triage-dev-clf` (multi-container: classifier + datadog-agent sidecar)
- `azurerm_container_app.feedback` — `ca-smb-inbox-triage-dev-fb` (multi-container)
- `azurerm_cosmosdb_account.main` — Serverless NoSQL, key auth disabled
- `azurerm_cosmosdb_sql_database.main` + `azurerm_cosmosdb_sql_container.{classifications,feedback}`
- `azurerm_cosmosdb_sql_role_assignment.uami_data` — UAMI → Cosmos Data Contributor
- `azurerm_logic_app_workflow.router` — routing workflow infrastructure

Plus the Datadog monitoring resources when both `dd_api_key` and `dd_app_key` are set,
under `module.inbox_triage_datadog[0]`:

- `datadog_monitor` × 3 — error-rate, p95-latency, and human-review-rate monitors
- `datadog_service_level_objective` — availability SLO
- `datadog_dashboard` — LLM observability dashboard

> **No `destroy` actions should appear.** If you see any, stop and investigate
> before proceeding.

### 5.4 Apply — pass 1: create the registry

> **Why two passes:** a Container App validates its image manifest at the moment the
> revision is created. The ACR is created by *this* Terraform, so on a clean deploy the
> registry is empty and the `azurerm_container_app` resources fail with
> `MANIFEST_UNKNOWN: manifest tagged by "latest" is not found`. So create the registry
> first (5.4), push the image into it (5.5), then apply everything else (5.6).

Create the registry and the resources it depends on:

```powershell
terraform apply -target='module.inbox_triage_azure.azurerm_container_registry.main'
```

Type `yes`. This stands up the resource group, ACR, the shared UAMI, and the AcrPull
role assignment — but **not** the Container Apps. Continue to 5.5 to build and push the
image; 5.6 then applies the rest.

### 5.5 Build & push the container image

Container Apps pulls the app code from ACR. **Build once, push, redeploy.** Both `classifier` and `feedback` Container Apps share a single image — `FUNCTION_TARGET` env var (set by Terraform) selects which handler `src/function_app.py` imports at startup.

#### 5.5.1 Capture ACR info from Terraform

```powershell
$ACR_NAME   = terraform -chdir=infra\environments\azure-dev output -raw acr_name
$ACR_SERVER = terraform -chdir=infra\environments\azure-dev output -raw acr_login_server
echo "ACR: $ACR_NAME at $ACR_SERVER"
```

> **Coming from pass 1?** After a `-target` apply, `terraform output` may warn that
> outputs are incomplete. `acr_name` / `acr_login_server` still resolve (the ACR exists).
> If they come back empty, read them from Azure directly:
> ```powershell
> $ACR_NAME   = "acrinboxtriagedev"
> $ACR_SERVER = (az acr show --name $ACR_NAME --query loginServer --output tsv)
> ```

#### 5.5.2 Authenticate to ACR

```powershell
az acr login --name $ACR_NAME
```

#### 5.5.3 Build the image (from project root)

```powershell
cd C:\path\to\smb-inbox-triage
docker build -f Dockerfile.azure -t "${ACR_SERVER}/smb-inbox-triage:latest" .
```

Build takes ~30–60s (longer on first run because pip needs to install deps). The Dockerfile uses `mcr.microsoft.com/azure-functions/python:4-python3.12` as base.

#### 5.5.4 Push to ACR

```powershell
docker push "${ACR_SERVER}/smb-inbox-triage:latest"
```

Confirm the tag landed:

```powershell
az acr repository show-tags --name $ACR_NAME --repository smb-inbox-triage --output table
```

> **First deploy:** the Container Apps don't exist yet, so stop here and run pass 2 (5.6) —
> Terraform creates the apps directly against this image. Steps 5.5.5–5.5.6 below are only
> for **subsequent rebuilds**, once the apps already exist.

#### 5.5.5 Force fresh revisions to pull the new image (subsequent rebuilds only)

When the image tag stays `:latest`, Container Apps does NOT automatically create a new revision on push — the spec hash is unchanged. Use `--revision-suffix` to force a new revision:

```powershell
az containerapp update --name ca-smb-inbox-triage-dev-clf --resource-group rg-smb-inbox-triage-dev `
  --container-name classifier `
  --image "${ACR_SERVER}/smb-inbox-triage:latest" `
  --revision-suffix v1

az containerapp update --name ca-smb-inbox-triage-dev-fb --resource-group rg-smb-inbox-triage-dev `
  --container-name feedback `
  --image "${ACR_SERVER}/smb-inbox-triage:latest" `
  --revision-suffix v1
```

Bump the suffix (`v2`, `v3`, …) every time you rebuild. Each revision needs a unique name.

#### 5.5.6 Verify both Container Apps came up healthy (subsequent rebuilds only)

```powershell
az containerapp revision list --name ca-smb-inbox-triage-dev-clf --resource-group rg-smb-inbox-triage-dev `
  --query "[?properties.active].{name:name, state:properties.runningState, replicas:properties.replicas}" --output table
```

Both apps should show `Running` and `1` replica.

> **Gotchas that will burn you if you skip them** (the TF takes care of these, but if you ever set env vars manually):
>
> - **`function_app.py` MUST exist at the wwwroot** — the Python v2 loader needs that exact filename. We ship a shim in `src/function_app.py` that imports the right module based on `FUNCTION_TARGET`. Without it the host reports `0 functions found (Custom)`.
> - **`FUNCTIONS_WORKER_RUNTIME=python` MUST be set** in the Container App env (the Azure Functions base image's default isn't honored consistently in Container Apps).
> - **`AzureWebJobsStorage` MUST be set** to a storage connection string, even for HTTP-only functions. Without it the host emits health-check warnings and may behave oddly.
> - **`FUNCTIONS_HTTPWORKER_PORT` MUST NOT be set anywhere** (not in Dockerfile, not in CA env). Setting it puts the Functions host into "custom-handler" mode and the Python worker is never invoked. The TF and Dockerfile both deliberately omit this.
> - **`PYTHON_ENABLE_OPENTELEMETRY=false`** — when true, the Functions Python worker tries to extract trace context via a global text-map propagator that our `observability/tracing.py` doesn't register, raising `AttributeError: 'NoneType' object has no attribute 'extract'`. Re-enable only after adding `set_global_textmap(TraceContextTextMapPropagator())` to `tracing.py`.

### 5.6 Apply — pass 2: create everything else

With the image in ACR, apply the full configuration to create the Container Apps and any remaining resources:

```powershell
terraform apply
```

Type `yes` when prompted. This takes **8–15 minutes** — Cosmos DB provisioning and
Key Vault creation are the slowest parts. The Container Apps now find
`smb-inbox-triage:latest` in ACR and provision cleanly.

> **If you see `ResourceNotFound` on the Key Vault access policy during apply:**
> This is a race condition where the Key Vault is created but Entra ID hasn't
> propagated the object ID yet. Re-run `terraform apply` — it's idempotent.

> **If you see `a resource ... already exists - to be managed via Terraform this
> resource needs to be imported`:** a previous apply created the Container App in Azure
> but the revision failed, so it's not in Terraform state. Delete the orphan and
> re-apply (it's empty/broken anyway):
> ```powershell
> az containerapp delete --name ca-smb-inbox-triage-dev-clf --resource-group rg-smb-inbox-triage-dev --yes
> az containerapp delete --name ca-smb-inbox-triage-dev-fb  --resource-group rg-smb-inbox-triage-dev --yes
> terraform apply
> ```

### 5.7 Save the outputs

Run this after pass 2 completes:

```powershell
terraform output
```

You'll see:

```
app_insights_instrumentation_key = <sensitive>
cosmos_account_endpoint          = "https://cosmos-inboxtriageddev.documents.azure.com:443/"
datastore_name                   = "classifications"
feedback_url                     = "https://func-smb-inbox-triage-dev-feedback.azurewebsites.net/api/feedback"
webhook_url                      = "https://func-smb-inbox-triage-dev-classifier.azurewebsites.net/api/webhook"
```

**Save these** — you need them in Phases 7 and 8.

```powershell
$WEBHOOK_URL      = terraform -chdir=infra\environments\azure-dev output -raw webhook_url
$FEEDBACK_URL     = terraform -chdir=infra\environments\azure-dev output -raw feedback_url
$COSMOS_ENDPOINT  = terraform -chdir=infra\environments\azure-dev output -raw cosmos_account_endpoint
```

### 5.8 Verify the Container Apps are running

```powershell
az containerapp list `
  --resource-group "rg-smb-inbox-triage-dev" `
  --query "[].{Name:name, State:properties.runningStatus, Revision:properties.latestRevisionName}" `
  --output table
```

Both `ca-smb-inbox-triage-dev-clf` and `ca-smb-inbox-triage-dev-fb` should show `Running` with a `Running` revision.

### 5.9 (Deprecated) Pre-2026-05-28 Flex Consumption zip deploy

<details>
<summary>Pre-2026-05-28 Flex Consumption zip flow — DO NOT USE unless rolling back</summary>

```powershell
# Create .build dir if it doesn't exist yet
New-Item -ItemType Directory -Path .build -ErrorAction SilentlyContinue | Out-Null

az functionapp deployment source config-zip `
  --name "func-smb-inbox-triage-dev-classifier" `
  --resource-group "rg-smb-inbox-triage-dev" `
  --src ".build\azure-classifier.zip"

az functionapp deployment source config-zip `
  --name "func-smb-inbox-triage-dev-feedback" `
  --resource-group "rg-smb-inbox-triage-dev" `
  --src ".build\azure-feedback.zip"
```

Each deploy takes 2–4 minutes. A successful deploy prints JSON with `"provisioningState": "Succeeded"`.

</details>

---

## Phase 6 — Populate Key Vault Secrets

Container Apps consume the OpenAI key, DD API key, and storage connection string **directly from Terraform variables** (Phase 5.1) — not from KV. Storing them in KV is still recommended as a source of truth for future deploys, audits, and rotations.

```powershell
$KV_NAME = (az keyvault list `
  --resource-group "rg-smb-inbox-triage-dev" `
  --query "[0].name" --output tsv)
echo $KV_NAME
# Expected: your-key-vault-name
```

### 6.1 (Optional) Store the Azure OpenAI API key

```powershell
az keyvault secret set --vault-name $KV_NAME --name "azure-openai-key" --value $AZURE_OPENAI_KEY
```

### 6.2 (Optional) Store the Datadog API key

```powershell
az keyvault secret set --vault-name $KV_NAME --name "datadog-api-key" --value $env:DD_API_KEY
```

### 6.3 (Optional) Store the Slack webhook URL

```powershell
az keyvault secret set --vault-name $KV_NAME --name "slack-webhook-url" --value "https://hooks.slack.com/services/YOUR/REAL/URL"
```

Slack notification calls are wrapped in `try/except` in `router/destinations.py` — a missing or invalid URL only suppresses notifications, it does not fail classification. Leave empty in dev.

### 6.4 Verify

```powershell
az keyvault secret list --vault-name $KV_NAME --query "[].name" --output table
```

---

## Phase 7 — Cosmos DB RBAC (automated)

The shared UAMI is granted `Cosmos DB Built-in Data Contributor` automatically by `azurerm_cosmosdb_sql_role_assignment.uami_data` in the TF module. No manual `az cosmosdb sql role assignment create` is needed.

Verify the binding exists:

```powershell
az cosmosdb sql role assignment list `
  --resource-group rg-smb-inbox-triage-dev `
  --account-name cosmos-inboxtriagedev `
  --output table
```

You should see a row with `RoleDefinitionId` ending in `00000000-0000-0000-0000-000000000002` and a `PrincipalId` matching `uami-smb-inbox-triage-dev`.

<details>
<summary>Pre-2026-05-28 manual Cosmos RBAC flow (deprecated)</summary>

Before the Container Apps migration, the Function App system-assigned identities needed manual role assignments because Terraform couldn't reference them safely.

The built-in Cosmos DB role for this is `Cosmos DB Built-in Data Contributor`
(role definition ID `00000000-0000-0000-0000-000000000002`).

### 7.1 Get the managed identity principal IDs

```powershell
$CLASSIFIER_PRINCIPAL = (az functionapp identity show `
  --name "func-smb-inbox-triage-dev-classifier" `
  --resource-group "rg-smb-inbox-triage-dev" `
  --query "principalId" --output tsv)

$FEEDBACK_PRINCIPAL = (az functionapp identity show `
  --name "func-smb-inbox-triage-dev-feedback" `
  --resource-group "rg-smb-inbox-triage-dev" `
  --query "principalId" --output tsv)

echo "Classifier: $CLASSIFIER_PRINCIPAL"
echo "Feedback:   $FEEDBACK_PRINCIPAL"
```

### 7.2 Get the Cosmos DB account resource ID

```powershell
$COSMOS_NAME = (az cosmosdb list `
  --resource-group "rg-smb-inbox-triage-dev" `
  --query "[0].name" --output tsv)

$COSMOS_ACCOUNT_ID = (az cosmosdb show `
  --name $COSMOS_NAME `
  --resource-group "rg-smb-inbox-triage-dev" `
  --query "id" --output tsv)

echo "Account: $COSMOS_NAME"
echo "ID:      $COSMOS_ACCOUNT_ID"
```

### 7.3 Assign the role to both managed identities

```powershell
# Classifier Function App
az cosmosdb sql role assignment create `
  --account-name $COSMOS_NAME `
  --resource-group "rg-smb-inbox-triage-dev" `
  --role-definition-id "00000000-0000-0000-0000-000000000002" `
  --principal-id $CLASSIFIER_PRINCIPAL `
  --scope $COSMOS_ACCOUNT_ID

# Feedback Function App
az cosmosdb sql role assignment create `
  --account-name $COSMOS_NAME `
  --resource-group "rg-smb-inbox-triage-dev" `
  --role-definition-id "00000000-0000-0000-0000-000000000002" `
  --principal-id $FEEDBACK_PRINCIPAL `
  --scope $COSMOS_ACCOUNT_ID
```

### 7.4 Verify the assignments

```powershell
az cosmosdb sql role assignment list `
  --account-name $COSMOS_NAME `
  --resource-group "rg-smb-inbox-triage-dev" `
  --query "[].{Role:roleDefinitionId,Principal:principalId}" `
  --output table
```

Should show two entries, one for each principal ID from 7.1.

</details>

---

## Phase 8 — Testing

Container Apps ingress is public by default at `/api/<function_name>`. No identity token is required — the endpoint URL is sufficient.

### 8.1 Get the webhook URL

```powershell
$WEBHOOK_URL = terraform -chdir=infra\environments\azure-dev output -raw webhook_url
echo $WEBHOOK_URL
```

### 8.2 Smoke test: synthetic payload

> **Body format:** The Azure Function accepts a **flat JSON object** directly —
> no envelope wrapping required.

```powershell
$BODY = '{
  "messageId":   "test-azure-001",
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
confidence     : 0.94
requires_human : False
summary        : Customer inquiring about missing shipping confirmation for order placed 5 days ago.
```

Save the `record_id` — you'll use it to verify the Cosmos DB write in the next step.

> **If you get a 500 back immediately:** The Function App cold start timed out before
> the Key Vault reference resolved. Wait 30 seconds and retry — the first invocation after
> a deploy triggers the KV reference resolution and may take 10–15 seconds.

### 8.3 Test additional intent categories

```powershell
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

Send-TestEmail -Subject "Interested in your pricing plans" -Body "We have 50 users and would like a demo."
Send-TestEmail -Subject "Question about my invoice" -Body "I was charged twice for last month."
Send-TestEmail -Subject "Filing chargeback today — final warning" -Body "Six weeks with no response. Disputing now."
```

Expected intents: `sales_inquiry`, `billing_question`, `urgent_escalation`.

### 8.4 Check Cosmos DB for stored records

```powershell
# Query the last 5 classifications using the Data Plane REST API via az rest
az cosmosdb sql container throughput show `
  --account-name $COSMOS_NAME `
  --resource-group "rg-smb-inbox-triage-dev" `
  --database-name "inbox-triage" `
  --name "classifications" `
  --query "resource.throughput" --output tsv 2>$null; `
Write-Host "Cosmos DB accessible"
```

For a full data view, use the Azure Portal:

```
https://portal.azure.com/#resource/subscriptions/YOUR_SUBSCRIPTION_ID/resourceGroups/rg-smb-inbox-triage-dev/providers/Microsoft.DocumentDB/databaseAccounts/cosmos-inboxtriageddev/dataExplorer
```

Navigate to **Data Explorer → inbox-triage → classifications → Items** to see the
stored classification records. Each record contains the full email metadata and
the AI's `ClassificationResult`.

> **CLI alternative:** The `az cosmosdb` CLI does not support querying documents
> directly without the connection key (which is disabled). Use the Portal's Data
> Explorer or write a Python script using `DefaultAzureCredential`:
>
> ```python
> # query_cosmos.py
> from azure.cosmos import CosmosClient
> from azure.identity import DefaultAzureCredential
>
> endpoint = "https://cosmos-inboxtriageddev.documents.azure.com:443/"
> client   = CosmosClient(endpoint, credential=DefaultAzureCredential())
> db       = client.get_database_client("inbox-triage")
> container = db.get_container_client("classifications")
>
> for item in container.query_items(
>     query="SELECT TOP 5 c.record_id, c.intent, c.classified_at FROM c ORDER BY c.classified_at DESC",
>     enable_cross_partition_query=True,
> ):
>     print(item)
> ```
> Requires: `pip install azure-cosmos azure-identity --break-system-packages`
> and `az login` so `DefaultAzureCredential` can pick up your credentials.

### 8.5 Test the feedback endpoint

```powershell
$FEEDBACK_URL = terraform -chdir=infra\environments\azure-dev output -raw feedback_url

$FEEDBACK_BODY = '{"record_id":"YOUR_RECORD_ID","corrected_intent":"sales_inquiry","reviewer":"reviewer-1"}'

# Without an HMAC signature — expect a 401, which proves the endpoint is live
Invoke-RestMethod -Uri $FEEDBACK_URL -Method POST `
  -Headers @{"Content-Type"="application/json"} `
  -Body $FEEDBACK_BODY
```

A 401 means the endpoint is live and signature enforcement is working correctly.

---

## Phase 9 — Observability

### 9.1 Stream live function logs via Azure CLI

```powershell
# Stream classifier logs (Ctrl+C to stop)
az webapp log tail `
  --name "func-smb-inbox-triage-dev-classifier" `
  --resource-group "rg-smb-inbox-triage-dev"
```

In a second terminal, send a test request:

```powershell
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
  "latency_ms": 820,
  "cloud": "azure",
  "model_id": "gpt-4o-mini"
}
```

### 9.2 Application Insights — built-in telemetry

Application Insights is provisioned by Terraform and wired to both Container Apps via `APPLICATIONINSIGHTS_CONNECTION_STRING`. Useful for built-in Azure failure analytics.

From the CLI, get the connection string:

```powershell
az monitor app-insights component show `
  --app "ai-smb-inbox-triage-dev" `
  --resource-group "rg-smb-inbox-triage-dev" `
  --query "connectionString" --output tsv
```

### 9.3 Datadog — OTLP via Agent sidecar

Topology:

```
classifier container
    │  OTLP/HTTP   http://localhost:4318
    ▼
datadog-agent sidecar (in same Container App)
    │  HTTPS/443  → Datadog intake
    ▼
Datadog APM + LLM Observability + Logs
```

The sidecar uses the DD API key from the Container App secret store (sourced from `TF_VAR_dd_api_key`). All sidecar config is in `azurerm_container_app.{classifier,feedback}` in `infra/modules/azure/main.tf` — env vars include:

- `DD_OTLP_CONFIG_RECEIVER_PROTOCOLS_HTTP_ENDPOINT=0.0.0.0:4318`
- `DD_HOSTNAME=smb-inbox-triage-dev-classifier` (Container Apps has no stable hostname; without this the agent core dies)
- `DD_SYSTEM_PROBE_ENABLED=false` (irrelevant in CA, emits noise)
- `DD_OTLP_CONFIG_{TRACES,METRICS,LOGS}_ENABLED=true`

After invoking the function a few times, check Datadog:

1. Open https://app.us5.datadoghq.com
2. **APM → Traces**, filter by `service:smb-inbox-triage env:dev cloud.provider:azure`
3. **Logs**, search `service:smb-inbox-triage env:dev cloud.provider:azure`

Logs and traces appear within seconds. **Metrics are a known issue** — span-derived metrics fire fine but custom counters may not appear yet; debug separately.

### 9.4 Verify the Datadog monitors and dashboard

The Datadog monitoring — error-rate, latency, and human-review-rate monitors, an
availability SLO, and an LLM observability dashboard — is part of the `azure-dev`
environment and was created by the `terraform apply` in Phase 5 (when both `dd_api_key`
and `dd_app_key` were set). It shares the `azure-dev` state; there is no separate
`datadog-dev` environment to apply.

Confirm the resources exist in Datadog:

- **Dashboards** → search `smb-inbox-triage` → open the LLM Observability dashboard
- **Monitors** → search `smb-inbox-triage` → error rate, p95 latency, and human-review rate monitors

Or from Terraform state:

```powershell
terraform -chdir=infra\environments\azure-dev state list | Select-String "datadog"
```

You should see `module.inbox_triage_datadog[0].datadog_monitor.*`,
`…datadog_service_level_objective.*`, and `…datadog_dashboard.*`.

> **If nothing shows up:** `dd_app_key` wasn't set when you ran the Phase 5 apply, so
> the monitoring module was count-gated to zero. Set both keys (Phase 5.1) and re-run
> `terraform apply` in `azure-dev`.

### 9.5 Expected non-fatal warnings in dev

| Warning message | Cause | Action needed |
|---|---|---|
| `Routing/dispatch failed (non-fatal): 'LINEAR_API_KEY'` | Linear env var not set | Set when wiring up Linear |
| `Routing/dispatch failed (non-fatal): 'HUBSPOT_API_KEY'` | HubSpot env var not set | Set when wiring up HubSpot |
| `Slack notify failed: Missing Slack webhook env var` | Per-channel webhook not set | Set channel-specific webhooks |
| `Duplicate message_id — skipping dispatch` | Same messageId sent twice | Expected — idempotency working |

None of these affect the classification result, Cosmos DB write, or HTTP 200 response.

---

## Phase 10 — Teardown (when done experimenting)

### 10.1 Destroy the Azure infrastructure

```powershell
cd infra\environments\azure-dev
terraform destroy
```

Type `yes`. This destroys the Function Apps, Cosmos DB, Key Vault, Storage Account,
App Insights, Logic App, Service Plan, and Resource Group — everything Terraform created.
When the Datadog keys were set, this same destroy also removes the Datadog monitors,
SLO, and dashboard, which now live in this state — there is no separate Datadog teardown step.

> **Key Vault soft delete:** By default, Azure Key Vault goes into a soft-deleted state
> for 90 days even after `terraform destroy`. The Terraform provider is configured with
> `purge_soft_delete_on_destroy = true`, so it will hard-purge the vault after deletion.
> This takes 30–60 seconds and runs automatically during `terraform destroy`.

### 10.2 Clean up non-Terraform resources

These were created manually in this guide and must be deleted separately:

```powershell
# Delete the Azure OpenAI resource (check first that you don't need it for other projects)
az cognitiveservices account delete `
  --name "inbox-triage-openai" `
  --resource-group "rg-tfstate"

# Delete the Terraform state infrastructure
az group delete --name "rg-tfstate" --yes --no-wait
```

> **`--no-wait`** returns immediately while the deletion runs in the background.
> The resource group and storage account may take 2–3 minutes to fully disappear.

### 10.3 Verify everything is gone

```powershell
az group list --query "[?starts_with(name, 'rg-smb-inbox-triage') || starts_with(name, 'rg-tfstate')].name" --output table
```

Should return an empty table.

---

## Troubleshooting

### `terraform init` fails with `AuthorizationFailed` on the backend

`ARM_ACCESS_KEY` is not set in this session. Re-set it:

```powershell
$env:ARM_ACCESS_KEY = (az storage account keys list `
  --resource-group "rg-tfstate" `
  --account-name "tfstateinboxtriage" `
  --query "[0].value" --output tsv)
```

Then re-run `terraform init`.

### `terraform apply` fails with `ResourceGroupNotFound`

The azurerm provider couldn't authenticate. Verify:

```powershell
echo $env:ARM_SUBSCRIPTION_ID
echo $env:ARM_TENANT_ID
az account show --query "id" --output tsv
```

If the subscription ID is blank or wrong, re-run Phase 1.2 and reset the env vars.

### Container App returns 502 immediately after deploy / new revision

**Most common cause:** cold start. The Container App needs ~45–60 seconds after a new revision is created before the Python worker is fully warm. The Functions host returns 502 from ingress during this window.

Wait 60 seconds and retry. The second request will be fast (~50–200ms).

If you see `502` persistently, pull container logs and look for the actual error:

```powershell
az containerapp logs show --name ca-smb-inbox-triage-dev-clf `
  --resource-group rg-smb-inbox-triage-dev --container classifier --tail 80 |
  Select-String "Executing|Executed|Traceback|Exception|AttributeError" -CaseSensitive:$false |
  Select-Object -Last 30
```

### Container App returns 500 with `AuthenticationError` from Azure OpenAI

**Cause:** `TF_VAR_azure_openai_api_key` was not set when `terraform apply` ran, OR the wrong (stale) key was passed. Container Apps does NOT resolve `@Microsoft.KeyVault(SecretUri=...)` — the literal value must be passed via `TF_VAR_azure_openai_api_key`. Re-set and re-apply:

```powershell
$env:TF_VAR_azure_openai_api_key = (az keyvault secret show --vault-name your-key-vault-name --name azure-openai-key --query value -o tsv)
terraform -chdir=infra\environments\azure-dev apply -auto-approve
```

### Container App reports `0 functions found (Custom)`

The Functions host isn't discovering our Python v2 decorators. Causes, in priority order:

1. **`function_app.py` missing from `/home/site/wwwroot`** — check `src/function_app.py` exists in the repo and was included in the docker build context.
2. **`FUNCTIONS_HTTPWORKER_PORT` env var is set** — this forces the host into "custom handler" mode and ignores the Python worker. Remove it from both the Dockerfile and Container App env.
3. **`FUNCTIONS_WORKER_RUNTIME` not set to `python`** — `az containerapp show` and confirm the value.
4. **The Python v2 decorator imports failed** — `azure_function_app.py` has a sys.modules dance that needs `classifier`, `adapters`, etc. on sys.path. If those failed, decorators don't register. Look for `ImportError` in the worker logs.

### Function App returns 500 with `AZURE_OPENAI_ENDPOINT not configured`

The `TF_VAR_azure_openai_endpoint` was not set when `terraform apply` ran, so the
`AZURE_OPENAI_ENDPOINT` app setting was set to an empty string. Fix:

```powershell
$env:TF_VAR_azure_openai_endpoint = (az cognitiveservices account show `
  --name "inbox-triage-openai" `
  --resource-group "rg-tfstate" `
  --query "properties.endpoint" --output tsv)

cd infra\environments\azure-dev
terraform apply
```

### Cosmos DB writes fail with `403 Forbidden` or `Unauthorized`

The Cosmos RBAC role assignment from Phase 7 hasn't propagated yet (Entra ID RBAC
changes take 1–5 minutes to propagate) **or** the assignment was skipped.

Verify the assignments exist:

```powershell
az cosmosdb sql role assignment list `
  --account-name $COSMOS_NAME `
  --resource-group "rg-smb-inbox-triage-dev" `
  --output table
```

If the table is empty, re-run Phase 7.3. If entries are there, wait 2 more minutes
for propagation and retry the test request.

### Key Vault returns `403` when Function App tries to read a secret

Two possible causes:

**A — Access policy was not applied to this Function App's managed identity**

Check the Key Vault access policies:

```powershell
az keyvault show `
  --name $KV_NAME `
  --query "properties.accessPolicies[*].{Object:objectId,Perms:permissions.secrets}" `
  --output table
```

The classifier's `principalId` (from Phase 7.1) should appear with `Get` permission.
If missing, add it:

```powershell
az keyvault set-policy `
  --name $KV_NAME `
  --object-id $CLASSIFIER_PRINCIPAL `
  --secret-permissions get
```

**B — Secret name in the Key Vault reference doesn't match what was created**

The Terraform hardcodes the secret name `azure-openai-key` for the API key reference.
If you created the secret with a different name, update either the Key Vault secret
name or `var.slack_webhook_secret_name` in `terraform.tfvars` and re-apply.

### `az webapp log tail` shows no output

The Function App may be in a cold state. Send a test request first to wake it up,
then run the log tail. If still empty, check that Application Insights is connected:

```powershell
az functionapp config appsettings list `
  --name "func-smb-inbox-triage-dev-classifier" `
  --resource-group "rg-smb-inbox-triage-dev" `
  --query "[?name=='APPINSIGHTS_INSTRUMENTATIONKEY'].value" --output tsv
```

If blank, the Terraform apply may have failed partway through — re-run `terraform apply`.

### Azure OpenAI returns `DeploymentNotFound`

The deployment name sent by the Python adapter must exactly match what you created
in Phase 4.3. The Terraform sets `AZURE_OPENAI_DEPLOYMENT = var.llm_model_id` which
defaults to `gpt-4o-mini` in `terraform.tfvars`. Verify the app setting is correct:

```powershell
az functionapp config appsettings list `
  --name "func-smb-inbox-triage-dev-classifier" `
  --resource-group "rg-smb-inbox-triage-dev" `
  --query "[?name=='AZURE_OPENAI_DEPLOYMENT'].value" --output tsv
```

Should print `gpt-4o-mini`. Verify the deployment exists in the Azure OpenAI resource:

```powershell
az cognitiveservices account deployment list `
  --name "inbox-triage-openai" `
  --resource-group "rg-tfstate" `
  --query "[].{Name:name,State:properties.provisioningState}" --output table
```

### Cold start latency is high (> 5 seconds)

Azure Functions Flex Consumption cold starts are typically 2–4 seconds (including
the KV reference resolution). This is a one-time cost per container instance.

Check invocation durations in Application Insights:

```powershell
az monitor app-insights query `
  --app "ai-smb-inbox-triage-dev" `
  --resource-group "rg-smb-inbox-triage-dev" `
  --analytics-query "requests | where timestamp > ago(1h) | project name, duration, success | order by timestamp desc | take 10"
```

The `duration` column shows total invocation time in milliseconds. Cold starts
typically show 3,000–5,000ms; warm invocations should be 800–2,000ms.

### `gpt-4o-mini` rate limits on initial testing

The default Global Standard quota for `gpt-4o-mini` is 150K tokens per minute —
more than enough for this workload. If you hit limits during batch testing, check
the quota assignment:

```powershell
az cognitiveservices account deployment show `
  --name "inbox-triage-openai" `
  --resource-group "rg-tfstate" `
  --deployment-name "gpt-4o-mini" `
  --query "{Capacity:sku.capacity, Unit:properties.rateLimits[0].renewalPeriod}" `
  --output json
```

`capacity: 100` means 100K tokens per minute. Adjust in the Portal under
**Azure OpenAI → Deployments → gpt-4o-mini → Edit** if you need more.

---

## Cost Reference (low volume, < 1,000 emails/month)

| Service | Estimated monthly cost |
|---------|----------------------|
| Azure Functions (Flex Consumption) | $0 — free grant: 100K executions + 100K GB-s |
| Azure OpenAI (GPT-4o-mini) | ~$0.012 per 1,000 emails (input + output tokens) |
| Cosmos DB (Serverless) | $0 — free tier: 1,000 RU/s + 25 GB storage |
| Key Vault | ~$0.03/month (3 secrets × $0.01/10K operations) |
| Application Insights | $0 — free tier: 5 GB/month ingest |
| Storage Account | < $0.01 (Function App state only) |
| Logic Apps Standard | ~$0.20/month (plan overhead; minimal at low volume) |
| **Total** | **~$0.25/month** |

> **Azure OpenAI pricing note:** GPT-4o-mini is priced at $0.15/MTok input and
> $0.60/MTok output (Global Standard, as of May 2026). A typical email classification
> uses ~900 input tokens and ~200 output tokens.

---

## What's Next

1. **Run the eval harness** — from Git Bash: `make eval-azure` to benchmark
   the model against the golden dataset
2. **Tune the Datadog monitors** — thresholds (error rate, p95 latency, human-review
   rate) and the SLO target live in `infra/modules/datadog/variables.tf`; adjust and
   re-apply `azure-dev`. To route alerts, wire the module's `notification_pagerduty` /
   `notification_slack_channel` inputs through `infra/environments/azure-dev/main.tf`.
3. **Connect Microsoft 365** — use the Graph API webhook push path (described in
   `ARCHITECTURE.md` section 4.1) to pipe real M365 email into the Azure Function

---

*Document created: 2026-05-25 | Region: eastus | Model: gpt-4o-mini | State backend: Azure Blob Storag