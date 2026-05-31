# Deploying SMB Inbox Triage to GCP — Step-by-Step

> **Who this is for:** Someone with SRE experience but no prior GCP deployments.
> All commands are written for **Windows PowerShell**. No backslash line continuations.
> GCP project ID used throughout: **`YOUR_PROJECT_ID`**

> ### ⚠️ ARCHITECTURE CHANGE — 2026-05-28
>
> GCP compute migrated from **Cloud Functions 2nd gen** (`google_cloudfunctions2_function`) → **Cloud Run V2 multi-container** (`google_cloud_run_v2_service`). The reason: we need a real Datadog Agent sidecar absorbing OTLP on `localhost:4318` for reliable trace export, which Cloud Functions 2nd gen does not support but Cloud Run V2 does.
>
> **What changed in this doc:**
> - Phase 4 (build) is now a **container image build via Cloud Build** (not a zip), pushed to Artifact Registry.
> - Phase 5 provisions Cloud Run V2 services with a `datadog-agent` sidecar each.
> - **New TF setting**: `run.googleapis.com/cpu-throttling = "false"` annotation on both services. Without it, the Cloud Run instance is throttled to ~0 CPU between requests and the DD Agent sidecar can't drain its queue, causing traces to drop. Scale-to-zero (`min_instance_count = 0`) is preserved — CPU is only continuous while the instance is alive.
> - **New IAM**: `artifactregistry.reader` on the classifier service account, so Cloud Run can pull the image. Without it the service create returns `Image ... not found` (which is actually a 401 disguised as a 404).
> - Gmail Watch (Phase 6) is unchanged.
> - The smoke test in Phase 7 now hits the Cloud Run URL directly with an authenticated `gcloud` identity token.

---

## Overview of what you're about to do

```
Phase 0  Prerequisites      Install the three tools you need
Phase 1  GCP project setup  Create project, enable billing, authenticate
Phase 2  Bootstrap          Create the Terraform state bucket (one-time)
Phase 3  Secrets            Enable Secret Manager + gather secret values (populated in 5.4)
Phase 4  Build & push image cloudbuild.yaml + Artifact Registry
Phase 5  Terraform          Provision Cloud Run V2 + sidecar + Cosmos + Pub/Sub
Phase 6  Gmail Watch        Wire your Gmail inbox to the pipeline
Phase 7  Test               Send an email and watch it flow through
Phase 8  Tear down          Destroy everything when done (optional)
```

Estimated time: **60–90 minutes** on a first run.

---

## Phase 0 — Prerequisites

You need three tools. Check what you already have:

```powershell
gcloud version
terraform version
python --version
```

Need: gcloud 450+, Terraform 1.6+, Python 3.12+.

### Install missing tools

**gcloud CLI** — download the installer from https://cloud.google.com/sdk/docs/install  
or in PowerShell (run as Administrator):

```powershell
(New-Object Net.WebClient).DownloadFile("https://dl.google.com/dl/cloudsdk/channels/rapid/GoogleCloudSDKInstaller.exe", "$env:Temp\GoogleCloudSDKInstaller.exe")
& $env:Temp\GoogleCloudSDKInstaller.exe
```

**Terraform:**

```powershell
winget install HashiCorp.Terraform
```

**Python 3.12:**

```powershell
winget install Python.Python.3.12
```

---

## Phase 1 — GCP Project Setup

### 1.1 Create the project

Go to https://console.cloud.google.com and create a new project.

- Project name: anything you like (display only)
- Project ID: `YOUR_PROJECT_ID` ← your actual project ID (already created)

### 1.2 Enable billing

GCP requires a billing account to run Cloud Functions, Firestore, and Vertex AI.
Go to **Billing → Link a billing account** in the console. Free tier covers most
dev usage — expect $0–$2/month at low traffic.

### 1.3 Authenticate

> **PowerShell note:** Use `--no-launch-browser` on all auth commands. This avoids
> the localhost callback issue. You'll get a URL to open, approve it in your browser,
> then paste the verification code back into PowerShell.

```powershell
gcloud auth login --no-launch-browser
```

Then set the default project:

```powershell
gcloud config set project YOUR_PROJECT_ID
```

Then create Application Default Credentials (Terraform needs these separately):

```powershell
gcloud auth application-default login --no-launch-browser
```

### 1.4 Verify

```powershell
gcloud config get-value project
```

Should print `YOUR_PROJECT_ID`.

```powershell
gcloud projects describe YOUR_PROJECT_ID
```

Should print project metadata. If this fails with "not found or permission denied",
billing isn't linked yet or the project doesn't exist — fix that in the console first.

### 1.5 Activate Vertex AI Generative AI (Agent Platform)

> **Why this is required:** GCP separates *API enablement* (what Terraform does) from
> *platform activation* (accepting terms and enabling the full generative AI feature set).
> Without this step every call to a Gemini model returns `404 Publisher Model not found`
> even though `aiplatform.googleapis.com` is enabled.

This is a one-time, manual step per project. It cannot be done via `gcloud` or Terraform.

1. Open the Agent Platform overview in the console:
   ```
   https://console.cloud.google.com/agent-platform/overview?project=YOUR_PROJECT_ID
   ```

2. A banner will appear: **"Enable APIs to access full platform capabilities."**
   Click the **Enable APIs** button.

3. A dialog lists ~15 APIs (Agent Registry, Agent Platform, Notebooks, etc.).
   Several will already be enabled (shown with a green checkmark). Click **Enable**
   to enable the remaining ones.

4. Wait 60–90 seconds for propagation, then verify the model is reachable:

   ```powershell
   $ACCESS_TOKEN = (gcloud auth print-access-token)
   $PROJECT_ID   = "YOUR_PROJECT_ID"

   Invoke-RestMethod -Method Post `
     -Uri "https://us-central1-aiplatform.googleapis.com/v1/projects/$PROJECT_ID/locations/us-central1/publishers/google/models/gemini-2.5-flash:generateContent" `
     -Headers @{ Authorization = "Bearer $ACCESS_TOKEN"; "Content-Type" = "application/json" } `
     -Body '{"contents":[{"role":"user","parts":[{"text":"Say OK"}]}]}'
   ```

   A successful response looks like:
   ```json
   { "candidates": [ { "content": { "parts": [ { "text": "OK" } ] } } ] }
   ```

   If you still get 404, wait another minute and retry — large API batches can take
   up to 2 minutes to propagate across all regions.

> **Note for future projects:** Add this step immediately after billing activation.
> Terraform apply will succeed without it, but every function invocation will fail
> at runtime.

> **Model availability by project age (as of March 2026):**
> `gemini-2.0-flash-001` and `gemini-2.0-flash-lite-001` are restricted to existing
> customers only. New projects must use `gemini-2.5-flash`, `gemini-2.5-flash-lite`,
> or newer. The `terraform.tfvars` in this repo already reflects this — do not
> downgrade to a `2.0` model on a new project, it will always 404.

---

## Phase 2 — Bootstrap: Terraform State Bucket

Terraform stores its state in a GCS bucket. This bucket must exist **before**
you run `terraform init`.

### 2.1 Enable the Storage API

```powershell
gcloud services enable storage.googleapis.com --project=YOUR_PROJECT_ID
```

### 2.2 Create the state bucket

```powershell
gcloud storage buckets create gs://YOUR_TFSTATE_BUCKET --project=YOUR_PROJECT_ID --location=us-central1 --uniform-bucket-level-access
```

> **If that name is taken** (bucket names are globally unique across all GCP):
> pick a different name like `YOUR_PROJECT_ID-tfstate` and then open
> `infra/environments/gcp-dev/main.tf` and update the `bucket =` line to match.

### 2.3 Enable versioning

```powershell
gcloud storage buckets update gs://YOUR_TFSTATE_BUCKET --versioning
```

---

## Phase 3 — Secrets

This stack uses two Secret Manager secrets: the Slack webhook URL
(`smb-inbox-triage-slack-webhook-url`) and the Datadog API key
(`smb-inbox-triage-dd-api-key`). **Terraform owns both secret *shells*** — they're
declared as `google_secret_manager_secret` resources in
`infra/modules/gcp/main.tf` (`slack_webhook` and `dd_api_key`). Terraform creates
the shells; it does **not** populate their values (that would mean putting secrets
in committed config).

Two consequences for ordering:

- **Do not `gcloud secrets create` these two secrets yourself.** Terraform creates
  them in Phase 5. Creating them by hand first makes `terraform apply` fail with an
  "already exists" error — Terraform can't create a resource that already exists
  outside its state.
- **The values must be populated *before* the full apply.** Cloud Run won't start a
  container that references a secret with no version, so the Cloud Run services
  (also created in Phase 5) would fail to deploy against empty secrets. Phase 5
  therefore stages the apply: create the shells → populate the versions → apply the
  rest. See **Phase 5.4**.

In this phase you only enable the API and gather the two values you'll write in
Phase 5.4.

### 3.1 Enable Secret Manager

```powershell
gcloud services enable secretmanager.googleapis.com --project=YOUR_PROJECT_ID
```

### 3.2 Get your Slack webhook URL

**Option A — you have a real Slack webhook URL:**

1. Go to https://api.slack.com/apps → **Create New App → From scratch**
2. Name it "Inbox Triage", pick your workspace
3. Go to **Incoming Webhooks → Activate → Add New Webhook to Workspace**
4. Choose a channel (e.g. `#inbox-triage-test`)
5. Copy the URL (starts with `https://hooks.slack.com/services/...`)

**Option B — skip Slack for now:** use the placeholder
`https://placeholder.invalid/no-slack`.

Keep whichever URL you choose handy — you write it into the secret in Phase 5.4.

### 3.3 Get your Datadog API key

Copy your key from https://us5.datadoghq.com/organization-settings/api-keys (a
32-character string), or have it loaded in `$env:DD_API_KEY`. You write it into the
`smb-inbox-triage-dd-api-key` secret in Phase 5.4.

---

## Phase 4 — Build the Function Source Package

Cloud Run V2 pulls images from Artifact Registry. Build via Cloud Build (no local Docker required) and push.

### 4.1 Make sure the Artifact Registry repo exists

If you're running Phase 4 BEFORE Phase 5, the AR repo doesn't exist yet — Cloud Build will create it implicitly only if you set `--substitutions=_REPO=...` for an existing path. The cleanest order is:

1. Phase 5.1 — set `TF_VAR_gcp_project_id`
2. Phase 5.2 — `terraform init`
3. **Partial apply** — create just the Artifact Registry first:
   ```powershell
   cd infra\environments\gcp-dev
   # Single-quote the -target value: PowerShell otherwise mangles it and
   # Terraform errors with `Invalid target "module"`.
   terraform apply '-target=module.inbox_triage_gcp.google_artifact_registry_repository.functions'
   ```
4. Phase 4.2 — Cloud Build into the now-existing repo
5. Phase 5.3 — full apply

### 4.2 Submit the Cloud Build

Run from the project root:

```powershell
cd C:\path\to\smb-inbox-triage
gcloud builds submit . `
  --config=cloudbuild.yaml `
  --substitutions=_TAG=latest `
  --project=YOUR_PROJECT_ID
```

Cloud Build reads `cloudbuild.yaml` (which uses the project's `Dockerfile`) to build and push the image to:

```
us-central1-docker.pkg.dev/YOUR_PROJECT_ID/smb-inbox-triage-dev-functions/smb-inbox-triage:latest
```

Build takes ~1–3 minutes. The Cloud Build console URL prints at the start if you want to watch.

### 4.3 Verify the image landed

```powershell
gcloud artifacts docker images list us-central1-docker.pkg.dev/YOUR_PROJECT_ID/smb-inbox-triage-dev-functions --include-tags
```

You should see one image with tag `latest`.

---

## Phase 5 — Terraform: Provision Everything

### 5.1 Set the project ID environment variable

Terraform reads the GCP project ID from an env var (kept out of committed files):

```powershell
$env:TF_VAR_gcp_project_id = "YOUR_PROJECT_ID"
```

> Add this to your PowerShell profile so it persists across sessions:
> ```powershell
> Add-Content $PROFILE '$env:TF_VAR_gcp_project_id = "YOUR_PROJECT_ID"'
> ```

### 5.2 Initialise Terraform

```powershell
cd infra\environments\gcp-dev
terraform init
```

This downloads the Google provider (~50 MB) and connects to the state bucket.
Expected ending: `Terraform has been successfully initialized!`

If you see `bucket not found`: the bucket name in `main.tf` doesn't match what
you created — update one to match the other.

### 5.3 Review the plan

```powershell
terraform plan
```

Read the output. You should see ~30–40 resources being created including:

- `google_project_service.apis` — enabling ~9 GCP APIs
- `google_service_account.classifier` — least-privilege SA for the function
- `google_project_iam_custom_role.firestore_classifier` — scoped Firestore role
- `google_artifact_registry_repository.functions` — Docker image registry
- `google_artifact_registry_repository_iam_member.classifier_image_pull` — **NEW** — classifier SA → `artifactregistry.reader`. Without this Cloud Run returns `Image ... not found` (which is actually a 401 disguised as 404).
- `google_cloud_run_v2_service.classifier` — main classifier service (multi-container: classifier + `datadog-agent` sidecar)
- `google_cloud_run_v2_service.feedback` — feedback correction service (multi-container)
- `google_firestore_database.main` — Firestore database
- `google_pubsub_topic.gmail_inbound` — topic Gmail pushes into
- `google_eventarc_trigger.gmail_to_classifier` — Pub/Sub → service wiring
- 8× intent Pub/Sub topics (sales, support, billing, etc.)
- Secret Manager IAM bindings

> **Note on the Cloud Run V2 services**: both have `template.annotations = { "run.googleapis.com/cpu-throttling" = "false" }` and `min_instance_count = 0`. CPU is always allocated for the lifetime of an instance (so the DD Agent sidecar can drain its queue between requests), but the service still scales to zero when idle (no cost when no traffic).

> **No `destroy` actions should appear.** If you see any, stop and check why
> before proceeding.

### 5.4 Create the secret shells and populate them (before the full apply)

The Cloud Run services reference the two Secret Manager secrets and won't deploy if
those secrets have no version. So create just the secret shells first, populate
their values, then do the full apply in 5.5.

**Step 1 — create only the secret shells:**

```powershell
# Single-quote each -target value — PowerShell otherwise mangles it and Terraform
# errors with `Invalid target "module"`.
terraform apply `
  '-target=module.inbox_triage_gcp.google_secret_manager_secret.slack_webhook' `
  '-target=module.inbox_triage_gcp.google_secret_manager_secret.dd_api_key'
```

Type `yes` when prompted. This creates the two empty secrets (and their dependency,
the API enablement) — nothing else.

**Step 2 — populate the Slack webhook value** (use your real URL from Phase 3.2, or
the placeholder):

```powershell
"https://hooks.slack.com/services/YOUR/REAL/URL" | `
  gcloud secrets versions add smb-inbox-triage-slack-webhook-url --data-file=- --project=YOUR_PROJECT_ID
```

**Step 3 — populate the Datadog API key** (no trailing newline — see the warning
below). With the key in `$env:DD_API_KEY`:

```powershell
[System.IO.File]::WriteAllBytes("$env:TEMP\dd_key.bin", [System.Text.Encoding]::UTF8.GetBytes($env:DD_API_KEY))
gcloud secrets versions add smb-inbox-triage-dd-api-key --data-file="$env:TEMP\dd_key.bin" --project=YOUR_PROJECT_ID
Remove-Item "$env:TEMP\dd_key.bin"
```

Or via bash/WSL:

```bash
echo -n "$DD_API_KEY" | gcloud secrets versions add smb-inbox-triage-dd-api-key \
  --data-file=- --project=YOUR_PROJECT_ID
```

> **Critical:** Don't let a trailing newline into the Datadog key. Secret Manager
> stores the exact bytes, and a trailing newline makes Datadog return 403 (key
> mismatch). `WriteAllBytes` + `UTF8.GetBytes` (above) writes the exact bytes —
> avoid `Set-Content`/`Out-File`, which can append a newline or BOM. The `-n` flag
> does the same for `echo`. Verify the byte count matches your key length
> (typically 32):
>
> ```bash
> gcloud secrets versions access latest --secret=smb-inbox-triage-dd-api-key \
>   --project=YOUR_PROJECT_ID | wc -c
> ```
>
> In PowerShell: `... | Measure-Object -Character`.

**Step 4 — confirm both secrets have a version:**

```powershell
gcloud secrets list --project=YOUR_PROJECT_ID
```

Both `smb-inbox-triage-slack-webhook-url` and `smb-inbox-triage-dd-api-key` should
be listed.

> **If a secret already exists outside Terraform** (e.g. you created it by hand with
> `gcloud secrets create`, or followed an earlier version of this guide), the
> targeted apply above — or the full apply in 5.5 — fails with:
> `Error 409: Secret [...] already exists`. Don't delete it (you'd lose the version).
> Import it into state instead, then continue:
>
> ```powershell
> terraform import `
>   'module.inbox_triage_gcp.google_secret_manager_secret.slack_webhook' `
>   projects/YOUR_PROJECT_ID/secrets/smb-inbox-triage-slack-webhook-url
> ```
>
> (Swap in `...google_secret_manager_secret.dd_api_key` /
> `.../secrets/smb-inbox-triage-dd-api-key` for the Datadog key.) After import,
> `terraform plan` will show a harmless in-place update adding the resource's labels.

### 5.5 Apply

```powershell
terraform apply
```

Type `yes` when prompted. This creates everything else (the targeted secret shells
from 5.4 already show as up-to-date). Takes **5–15 minutes** — API enablement alone
takes 2–3 minutes, Cloud Function builds take another 2–4 minutes each.

> **If a Cloud Run service fails with** `Permission denied on secret: .../versions/latest ...
> must be granted 'Secret Manager Secret Accessor'`: the service was created before its
> `secretmanager.secretAccessor` IAM binding finished propagating. The module pins the
> Cloud Run services to `depends_on` those bindings (so a clean apply orders them
> correctly), but if you hit it on a partial/older state, just **re-run
> `terraform apply`** — the binding exists by then and the service creates cleanly.
> Wait ~30–60s first if it races a second time.

### 5.6 Save the outputs

When apply finishes, copy the outputs:

```powershell
terraform output
```

You'll see something like:

```
feedback_url       = "https://us-central1-YOUR_PROJECT_ID.cloudfunctions.net/smb-inbox-triage-dev-feedback"
gmail_pubsub_topic = "projects/YOUR_PROJECT_ID/topics/smb-inbox-triage-dev-gmail-inbound"
webhook_url        = "https://us-central1-YOUR_PROJECT_ID.cloudfunctions.net/smb-inbox-triage-dev-classifier"
```

**Save these** — you need them in Phases 6 and 7.

### 5.7 Verify the Cloud Run services deployed

```powershell
gcloud run services list --region=us-central1 --project=YOUR_PROJECT_ID
```

Both `smb-inbox-triage-dev-classifier` and `smb-inbox-triage-dev-feedback` should show a healthy revision.

### 5.8 Stuck-revision recovery

If you ever see a Cloud Run create/update hang or fail with image-pull errors after a rebuild, the most common causes (with fixes) are:

| Symptom | Cause | Fix |
|---|---|---|
| `Image '...:latest' not found` on first create | Image pushed AFTER service tried to start | Image now exists; `terraform untaint` the service then re-apply: `terraform untaint module.inbox_triage_gcp.google_cloud_run_v2_service.classifier` |
| `Image '...:latest' not found` after AR repo creation | `artifactregistry.reader` IAM not propagated yet | Wait 30s, re-apply |
| Service has `tainted` state in TF, can't destroy | `deletion_protection=true` default | `terraform untaint <addr>` then re-apply (the resource itself stays) |
| Endless "Still creating..." | Image pull retrying because IAM/digest mismatch | Ctrl+C, fix the underlying cause, re-apply (Terraform reconciles partial state)

---

## Phase 6 — Wire Gmail to the Pipeline

Gmail notifies you of new email via Pub/Sub. It doesn't send the full message —
it sends a notification containing a `historyId`. Your Cloud Function then calls
the Gmail API with that `historyId` to fetch the actual email content. This
requires OAuth credentials (not a service account).

### 6.1 Enable the Gmail API

```powershell
gcloud services enable gmail.googleapis.com --project=YOUR_PROJECT_ID
```

### 6.2 Create OAuth 2.0 credentials

1. Go to **GCP Console → APIs & Services → Credentials**
2. If a consent screen prompt appears:
   - User Type: **External**
   - App name: `Inbox Triage`
   - Add `you@example.com` as a test user
   - Add scope: `https://www.googleapis.com/auth/gmail.readonly`
3. Click **Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Name: `inbox-triage-local`
4. Download the JSON → rename it to `gmail-oauth-credentials.json`
5. Place it in the project root (`smb-inbox-triage/`)

> This file is in `.gitignore`. Do not commit it.

### 6.3 Install the OAuth library

```powershell
pip install google-auth-oauthlib google-api-python-client
```

### 6.4 Authenticate and save the token

Create `get_gmail_token.py` in the project root:

```python
# get_gmail_token.py
from google_auth_oauthlib.flow import InstalledAppFlow
import json

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

flow = InstalledAppFlow.from_client_secrets_file("gmail-oauth-credentials.json", SCOPES)

# run_console() prints a URL — open it in your browser, approve, paste the
# verification code back here.  No localhost listener needed.
creds = flow.run_console()

token_data = {
    "token":         creds.token,
    "refresh_token": creds.refresh_token,
    "token_uri":     creds.token_uri,
    "client_id":     creds.client_id,
    "client_secret": creds.client_secret,
    "scopes":        list(creds.scopes),
}

with open("gmail-token.json", "w") as f:
    json.dump(token_data, f, indent=2)

print("Saved gmail-token.json")
```

```powershell
python get_gmail_token.py
```

The script prints a URL. Open it in your browser, sign in as `you@example.com`,
grant access, then copy the verification code and paste it back into PowerShell.
`gmail-token.json` will be written to the project root.

> This file is in `.gitignore`. Do not commit it.

### 6.5 Store OAuth credentials in Secret Manager

```powershell
gcloud secrets create gmail-oauth-credentials --data-file=gmail-oauth-credentials.json --project=YOUR_PROJECT_ID
```

```powershell
gcloud secrets create gmail-oauth-token --data-file=gmail-token.json --project=YOUR_PROJECT_ID
```

### 6.6 Grant the classifier SA access to both secrets

```powershell
$CLASSIFIER_SA = gcloud iam service-accounts list --project=YOUR_PROJECT_ID --filter="displayName:Inbox Triage Classifier" --format="value(email)"
echo $CLASSIFIER_SA
```

```powershell
gcloud secrets add-iam-policy-binding gmail-oauth-credentials --member="serviceAccount:$CLASSIFIER_SA" --role="roles/secretmanager.secretAccessor" --project=YOUR_PROJECT_ID
```

```powershell
gcloud secrets add-iam-policy-binding gmail-oauth-token --member="serviceAccount:$CLASSIFIER_SA" --role="roles/secretmanager.secretAccessor" --project=YOUR_PROJECT_ID
```

### 6.7 Register the Gmail Watch

This tells Gmail: "push me a notification whenever my inbox changes."
**Watch registrations expire after 7 days** — you need to re-run this weekly.

Create `register_gmail_watch.py` in the project root:

```python
# register_gmail_watch.py
import json
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

PUBSUB_TOPIC = "projects/YOUR_PROJECT_ID/topics/smb-inbox-triage-dev-gmail-inbound"

with open("gmail-token.json") as f:
    t = json.load(f)

creds = Credentials(
    token=t["token"],
    refresh_token=t["refresh_token"],
    token_uri=t["token_uri"],
    client_id=t["client_id"],
    client_secret=t["client_secret"],
    scopes=t["scopes"],
)

service = build("gmail", "v1", credentials=creds)
response = service.users().watch(
    userId="me",
    body={"topicName": PUBSUB_TOPIC, "labelIds": ["INBOX"]},
).execute()

print("Watch registered:")
print(json.dumps(response, indent=2))
print()
print("NOTE: expires in 7 days — re-run this script weekly to keep it active.")
```

```powershell
python register_gmail_watch.py
```

Expected output:

```json
{
  "historyId": "1234567",
  "expiration": "1749000000000"
}
```

If you see a 403 here, the IAM binding from Phase 6.6 hasn't propagated yet —
wait 60 seconds and try again.

---

## Phase 7 — Testing

### 7.0 Session setup (run once per terminal session)

```powershell
# Identity token — expires after 1 hour; re-run if you start getting 403s
$TOKEN = gcloud auth print-identity-token

# Webhook URL from Terraform state
$WEBHOOK_URL = terraform -chdir=infra\environments\gcp-dev output -raw webhook_url
echo $WEBHOOK_URL
```

### 7.1 Send test emails — all intents

The helper function below constructs a Pub/Sub-wrapped payload identical to what
Gmail push delivers. Each call returns a `record_id` you can use in 7.3 to look
up the Firestore record.

```powershell
# ── Helper: build + send a synthetic email via the webhook ──────────────────
function Send-TestEmail {
    param(
        [string]$MsgId,
        [string]$FromAddress,
        [string]$FromName,
        [string]$Subject,
        [string]$Body
    )
    $email = @{
        messageId   = $MsgId
        fromAddress = $FromAddress
        fromName    = $FromName
        toAddress   = "inbox@test.com"
        subject     = $Subject
        bodyText    = $Body
        receivedAt  = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ")
        source      = "test"
    } | ConvertTo-Json -Compress

    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($email))

    $payload = @{
        message = @{
            data        = $b64
            messageId   = "ps-$MsgId"
            publishTime = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ")
        }
        subscription = "projects/YOUR_PROJECT_ID/subscriptions/test"
    } | ConvertTo-Json -Depth 5

    Write-Host "`n── $MsgId ──────────────────────────────────" -ForegroundColor Cyan
    Invoke-RestMethod -Uri "$WEBHOOK_URL/webhook" `
        -Method POST `
        -Headers @{ Authorization = "Bearer $TOKEN"; "Content-Type" = "application/json" } `
        -Body $payload
}
```

Now send one message per intent:

```powershell
# support_request — shipping / order problem
Send-TestEmail `
    -MsgId      "test-support-001" `
    -FromAddress "sarah.jones@example.com" `
    -FromName    "Sarah Jones" `
    -Subject     "URGENT: Order #4821 hasn't arrived — it's been 3 weeks" `
    -Body        "Hi, I placed order #4821 three weeks ago and it still hasn't arrived. I've emailed twice with no reply. This is really frustrating. I need this resolved immediately or I'll dispute the charge."

# sales_inquiry — prospect asking about pricing / demo
Send-TestEmail `
    -MsgId      "test-sales-001" `
    -FromAddress "mike.chen@acmecorp.com" `
    -FromName    "Mike Chen" `
    -Subject     "Interested in your service — pricing for a team of 25?" `
    -Body        "Hi, I came across your product and I'm evaluating options for our team. We have about 25 users. Do you offer volume pricing? Could we schedule a 30-minute demo this week? I'd like to see the reporting features specifically."

# billing_question — unexpected charge or invoice dispute
Send-TestEmail `
    -MsgId      "test-billing-001" `
    -FromAddress "jane.smith@example.com" `
    -FromName    "Jane Smith" `
    -Subject     "Question about my April invoice — unexpected charge of \$149" `
    -Body        "Hello, I received my invoice for April and there's a charge of \$149 that I don't recognise. My plan is \$99/month. Could you clarify what this extra charge is for and issue a refund if it was applied in error?"

# spam / unsubscribe — marketing blast with no intent
Send-TestEmail `
    -MsgId      "test-spam-001" `
    -FromAddress "noreply@deals-blast.io" `
    -FromName    "Deals Team" `
    -Subject     "60% OFF today only — your exclusive offer inside!" `
    -Body        "Don't miss our biggest sale ever! Click here to claim your 60% discount. This offer expires at midnight. To stop receiving emails click unsubscribe below."

# escalation / chargeback threat — high urgency support
Send-TestEmail `
    -MsgId      "test-escalation-001" `
    -FromAddress "robert.kim@example.com" `
    -FromName    "Robert Kim" `
    -Subject     "Filing chargeback TODAY unless I hear back — final warning" `
    -Body        "I have been trying to get a refund for six weeks. Nobody responds. I am contacting my bank to dispute the charge today and will be leaving 1-star reviews on Google, Trustpilot, and every platform I can find. You have until end of business to respond."

# general_inquiry — low-urgency informational question
Send-TestEmail `
    -MsgId      "test-inquiry-001" `
    -FromAddress "alex.taylor@example.com" `
    -FromName    "Alex Taylor" `
    -Subject     "Do you integrate with Zapier?" `
    -Body        "Hi, quick question — does your platform have a Zapier integration? I want to connect it to my CRM. Also, is there an API I can call directly? Thanks."
```

Expected response for each:

```json
{"status": "ok", "record_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"}
```

Save the `record_id` values — use them in 7.3 to verify each record in Firestore.

### 7.2 Test with a real Gmail email (end-to-end path)

Send an email to `you@example.com` from any address. Allow 30–60 seconds for
the Gmail → Pub/Sub → Eventarc → Function chain.

| Subject to send | Expected intent |
|---|---|
| `URGENT: Order #999 hasn't arrived after 3 weeks` | `support_request` |
| `Interested in your pricing plans for 25 users` | `sales_inquiry` |
| `Question about my invoice — unexpected charge` | `billing_question` |
| `Filing chargeback today — final warning` | escalated `support_request` |
| `Do you have a Zapier integration?` | `general_inquiry` |

### 7.3 Query Firestore — last N records (DynamoDB scan equivalent)

> `gcloud firestore documents` doesn't exist — `gcloud firestore` only handles
> database-level operations.  Use one of the options below.

**Option A — last 10 records, formatted table (most useful):**

Build the script in a single-quote here-string (`@'...'@` — no variable expansion,
so the Python is passed verbatim), write it to a temp file with `WriteAllText`
(UTF-8, no BOM), and run it. This avoids piping to `python -` over stdin, which
PowerShell does not handle reliably.

```powershell
$py = @'
import google.cloud.firestore as fs
db   = fs.Client(project="YOUR_PROJECT_ID", database="smb-inbox-triage-dev-db")
docs = list(db.collection("classifications")
              .order_by("classified_at", direction=fs.Query.DESCENDING)
              .limit(10).stream())
print(f"{'record_id':<38}  {'intent':<20}  {'urgency':<10}  {'conf':>5}  classified_at")
print("-" * 100)
for d in docs:
    r   = d.to_dict()
    res = r.get("result", {})
    print(f"{d.id:<38}  {res.get('intent',''):<20}  {res.get('urgency',''):<10}  "
          f"{res.get('confidence',0):>5.2f}  {r.get('classified_at','')}")
'@
$tmp = "$env:TEMP\fs_query.py"
[System.IO.File]::WriteAllText($tmp, $py)
python $tmp
Remove-Item $tmp
```

Sample output:

```
record_id                               intent                urgency     conf  classified_at
----------------------------------------------------------------------------------------------------
3f2a1b9c-...                            support_request       high        0.97  2026-05-25 14:32:11+00:00
7c8d4e2a-...                            sales_inquiry         low         0.94  2026-05-25 14:31:58+00:00
```

**Option B — fetch a specific record by record_id:**

```powershell
$id = "YOUR_RECORD_ID_HERE"
$py = @"
import google.cloud.firestore as fs, json
db  = fs.Client(project='YOUR_PROJECT_ID', database='smb-inbox-triage-dev-db')
doc = db.collection('classifications').document('$id').get()
print(json.dumps(doc.to_dict(), default=str, indent=2) if doc.exists else 'NOT FOUND')
"@
$tmp = "$env:TEMP\fs_get.py"
[System.IO.File]::WriteAllText($tmp, $py)
python $tmp
Remove-Item $tmp
```

**Option C — console (no Python required):**

```
https://console.cloud.google.com/firestore/databases/smb-inbox-triage-dev-db/data/panel/classifications?project=YOUR_PROJECT_ID
```

> **Prerequisite:** `pip install google-cloud-firestore` and
> `gcloud auth application-default login` for Options A and B.

### 7.4 Stream the function logs

```powershell
gcloud functions logs read smb-inbox-triage-dev-classifier --project=YOUR_PROJECT_ID --gen2 --limit=50
```

A successful classification log looks like:

```json
{
  "level": "INFO",
  "message": "Classification complete",
  "intent": "support_request",
  "urgency": "high",
  "confidence": 0.97,
  "latency_ms": 1834,
  "cloud": "gcp",
  "model_id": "gemini-2.5-flash"
}
```

### 7.5 Verify Datadog telemetry (logs + APM traces)

The GCP deployment sends telemetry via direct OTLP/HTTP to Datadog's intake
(`https://otlp.us5.datadoghq.com`, port 443). No Lambda Extension — instead, the
`DD_API_KEY` secret env var provides auth via the `DD-API-KEY` HTTP header.

**Step 1 — Confirm the tracer initialised with a key**

Search Cloud Logging for the cold-start log line emitted by `tracing.py`:

```powershell
gcloud logging read 'resource.type="cloud_run_revision" AND textPayload:"OTel tracer initialised" AND resource.labels.service_name="smb-inbox-triage-dev-classifier"' `
  --project=YOUR_PROJECT_ID --limit=5 --format=json
```

The log line looks like:

```
OTel tracer initialised: endpoint=https://otlp.us5.datadoghq.com/v1/traces has_api_key=True
```

If `has_api_key=False` then `DD_API_KEY` is not reaching the container — check
that the secret version was populated (Phase 5.4) and re-deploy:

```powershell
cd infra\environments\gcp-dev
terraform apply
```

**Step 2 — Check for OTel export errors**

Any OTLP HTTP POST failure is logged at WARNING by the application container. Look
for it — but scope the query so it returns *app* logs from the *recent* window, not
historical infrastructure events:

```powershell
gcloud logging read 'resource.type="cloud_run_revision" AND severity>=WARNING AND resource.labels.service_name:"smb-inbox-triage-dev" AND NOT logName:"cloudaudit.googleapis.com"' `
  --project=YOUR_PROJECT_ID --freshness=1h --limit=20 --format=json
```

> **Why `NOT logName:"cloudaudit..."` and `--freshness`:** without them, this query
> also returns `cloudaudit.googleapis.com/system_event` `CreateService` records —
> the `SecretsAccessCheckFailed` errors from any failed revision during initial
> deploy (secret-not-yet-populated, or the IAM-propagation race in 5.5). Those are
> permanent point-in-time audit events, so they keep surfacing even after a later
> revision came up healthy and telemetry is flowing. Excluding the audit log and
> bounding the window shows only live application warnings. To check *current*
> health instead of log history, use:
>
> ```powershell
> gcloud run services describe smb-inbox-triage-dev-classifier --region=us-central1 `
>   --project=YOUR_PROJECT_ID `
>   --format="value(status.conditions[0].type, status.conditions[0].status, status.latestReadyRevisionName)"
> ```
>
> `Ready  True  <revision>` means the service is serving regardless of older error
> events in the log.

Common errors and fixes:

| Log message | Cause | Fix |
|---|---|---|
| `has_api_key=False` | Secret not populated | Phase 5.4 |
| `OTel log handler setup failed` | OTel log packages not installed | Check `requirements.txt` install in Cloud Build |
| `OTel trace flush timed out` | Network blocked or endpoint wrong | Check VPC egress rules allow HTTPS/443 to `otlp.us5.datadoghq.com` |

**Step 3 — Verify logs in Datadog**

After two or three invocations, check Datadog Logs:
```
https://us5.datadoghq.com/logs?query=service%3Asmb-inbox-triage+env%3Adev&cols=host%2Cservice
```

Logs arrive within seconds of each invocation (synchronous OTLP flush before
the function returns). Unlike AWS Lambda Extension (async flush on container
recycle), GCP calls `force_flush()` in-band so logs appear immediately.

**Step 4 — Verify APM traces in Datadog**

```
https://us5.datadoghq.com/apm/traces?query=env%3Adev+service%3Asmb-inbox-triage
```

Spans also flush synchronously (see above) so they appear within seconds too.
If you see logs but no traces, the trace exporter specifically is failing — look
for WARNING lines containing `trace flush` in Cloud Logging.

> **Architecture note:** Unlike AWS (where the Lambda Extension provides a fallback
> log path via CloudWatch), GCP has no automatic log forwarding to Datadog.
> The OTLP path is the only one. If `OBSERVABILITY_ENABLED=false` or the DD API key
> is missing, there is zero Datadog visibility — no fallback.

---

## Phase 8 — Teardown (when done experimenting)

```powershell
cd infra\environments\gcp-dev
terraform destroy
```

Type `yes`. This destroys all Cloud Functions, Firestore, Pub/Sub topics, service
accounts, and IAM bindings.

The state bucket and secrets are **not** managed by Terraform — clean them up
manually if you're done with the project:

```powershell
gcloud secrets delete smb-inbox-triage-slack-webhook-url --project=YOUR_PROJECT_ID
gcloud secrets delete gmail-oauth-credentials --project=YOUR_PROJECT_ID
gcloud secrets delete gmail-oauth-token --project=YOUR_PROJECT_ID
```

```powershell
gcloud storage buckets delete gs://YOUR_TFSTATE_BUCKET
```

Stop the Gmail Watch (prevents stray notifications):

```python
# stop_gmail_watch.py
import json
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

with open("gmail-token.json") as f:
    t = json.load(f)

creds = Credentials(token=t["token"], refresh_token=t["refresh_token"],
    token_uri=t["token_uri"], client_id=t["client_id"],
    client_secret=t["client_secret"], scopes=t["scopes"])

service = build("gmail", "v1", credentials=creds)
service.users().stop(userId="me").execute()
print("Gmail watch stopped.")
```

```powershell
python stop_gmail_watch.py
```

---

## Troubleshooting

### `terraform apply` fails with "API not enabled"

GCP can take a few minutes to propagate API enablement. Re-run `terraform apply`
— it's idempotent and will resume from where it stopped.

### Cloud Function shows `DEPLOY_IN_PROGRESS` for more than 10 minutes

```powershell
gcloud builds list --project=YOUR_PROJECT_ID --limit=5
```

If a build is stuck, cancel it in the console and re-run `terraform apply`.

### Function returns 403

Your identity token expired (they last 1 hour). Refresh:

```powershell
$TOKEN = gcloud auth print-identity-token
```

### Gmail Watch returns 403

Verify the Pub/Su