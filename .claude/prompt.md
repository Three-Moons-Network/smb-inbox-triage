# SMB AI Automation — Build Prompt Chain

This file contains a three-stage prompt chain for building a production-ready
cross-cloud AI pipeline from scratch. Using it as written should eliminate the
iterative adversarial-review → fix → re-review loop that was required when the
`smb-inbox-triage` project was first constructed.

**Efficiency note:** Three separate prompts (rather than one massive prompt)
keeps each context window focused on a single concern, produces higher-quality
output, and avoids the model losing track of earlier instructions in very long
sessions. Each prompt is self-contained and can be re-run independently if only
one layer needs to be rebuilt.

---

## Prompt 1 — Application Code

> **Use for:** Python source code, tests, evals, and CI workflows.
> **Does not cover:** Terraform / infrastructure.

```
You are building a production-grade cross-cloud AI pipeline for a small/medium
business automation product. The full specification follows.

──────────────────────────────────────────────────────────────────────────────
PROJECT
──────────────────────────────────────────────────────────────────────────────
Name: <your-project-name>
Purpose: <one-sentence description of what the pipeline does>
Owner identity: <your GitHub username> — no pushes to any other identity.

Cloud targets (implement all three):
  • AWS     — Lambda (arm64, Python 3.12) + Bedrock (Claude Haiku)
  • Azure   — Azure Functions (Python 3.12) + Azure OpenAI (GPT-4o-mini)
  • GCP     — Cloud Functions gen2 (Python 3.12) + Vertex AI (Gemini Flash)

──────────────────────────────────────────────────────────────────────────────
REQUIRED DIRECTORY LAYOUT
──────────────────────────────────────────────────────────────────────────────
src/
  classifier/
    models.py       — Pydantic v2 models (EmailMessage, ClassificationResult,
                       ClassificationRecord, RoutingDecision)
    prompts.py      — SYSTEM_PROMPT + build_user_message()
    handler.py      — classify() function + LLMAdapter Protocol
  adapters/
    aws_bedrock.py  — BedrockAdapter (Converse API + tool_use)
    azure_openai.py — AzureOpenAIAdapter (Chat Completions + json_schema)
    gcp_vertex.py   — VertexAIAdapter (GenerativeModel + response_schema)
  router/
    destinations.py — dispatch(), one handler per routing destination
  feedback/
    handler.py      — record_feedback()
  observability/
    tracing.py      — OTel + Datadog instrumentation (see OBSERVABILITY section)
tests/
  test_classifier.py
  test_router.py
  test_feedback.py
evals/
  eval_classifier.py
.github/workflows/
  ci.yml

──────────────────────────────────────────────────────────────────────────────
IMPLEMENTATION DISCIPLINE — follow these rules for every file you write
──────────────────────────────────────────────────────────────────────────────
D1  — SWEEP ALL INSTANCES. When applying a fix or pattern (e.g. tracing_config,
      retry logic, timeout, env-var validation), grep the entire codebase and
      apply it to EVERY matching location before moving on. Never stop at the
      first example found.

D2  — PAIRED CONTROLS. Every control has a logical counterpart that must also
      be implemented:
      • Add retry logic       → also add jitter AND idempotency keys (or
                                 pre-write dedupe) for every non-idempotent
                                 endpoint touched by that retry.
      • Add auth/validation   → also add an integration test with a
                                 malicious/invalid input that proves the
                                 mitigation holds.
      • Add env-var secret    → also validate its presence at module import
                                 time (not at first call).
      • Add OTel exporter     → wrap construction in try/except; degrade to
                                 no-op rather than crashing cold start.

D3  — VALIDATE AT MODULE LOAD. All required environment variables must be
      checked when the module is first imported, not when the first request
      arrives. Raise a descriptive ValueError naming the variable and its
      purpose. This surfaces misconfiguration at deploy time, not in traffic.

D4  — BOUND IN-MEMORY STATE. Any in-memory set, dict, or cache must have a
      maximum size. Use a capped LRU structure (e.g. collections.OrderedDict
      with maxlen logic, or functools.lru_cache). Document the eviction
      strategy in a code comment.

D5  — NO __import__() CALLS. Resolve circular imports with a dedicated
      types.py or constants.py module. String-based dynamic imports are
      invisible to static analysis and break on rename.

D6  — RETRY WITH JITTER. All exponential back-off loops must add ±25%
      random jitter to the computed wait time:
        wait = base_delay * (2 ** attempt)
        wait = wait * random.uniform(0.75, 1.25)
      This prevents thundering-herd synchronization under rate-limit storms
      when multiple instances back off simultaneously.

──────────────────────────────────────────────────────────────────────────────
SECURITY — apply every item without exception
──────────────────────────────────────────────────────────────────────────────
S1  — No secrets in source code, env files, or any committed file.
      All secrets come from Secrets Manager / Key Vault / Secret Manager or
      environment variables set at runtime.
S2  — No wildcard (*) IAM actions or resources. Scope every statement to the
      minimum required ARN.
S3  — Pydantic models use model_validate(), not model_construct().
      All external inputs are validated before use.
S4  — Raw LLM output is NEVER logged. Log only parse error messages (≤200 chars).
S5  — datetime.now(timezone.utc) everywhere. utcnow() is banned.
S6  — All HTTP clients use explicit timeouts (≤30 s).
S7  — Retry transient errors (rate limits, throttling) with exponential back-off
      plus jitter (see D6), max 3 attempts. Re-raise as domain exceptions
      (LLMInvocationError) on exhaustion so the caller can decide whether to
      DLQ or return 4xx/5xx.
S8  — MAX_TOKENS finish reason raises LLMInvocationError rather than returning
      truncated (and likely invalid) JSON.
S9  — temperature=0 on all LLM calls for deterministic classification output.
S10 — LLM JSON response schemas must be hand-rolled (no Pydantic model_json_schema()
      output). Reason: each provider rejects different JSON Schema features.
      • Bedrock tool_use:     no $ref/$defs; plain type+enum+properties
      • Azure OpenAI strict:  no anyOf; type:["string","null"] for nullable;
                               additionalProperties:false on every object
      • Vertex response_schema: no anyOf/oneOf/$ref; use nullable:true

──────────────────────────────────────────────────────────────────────────────
OBSERVABILITY — implement in src/observability/tracing.py and all src modules
──────────────────────────────────────────────────────────────────────────────
O1  — Use opentelemetry-sdk, opentelemetry-exporter-otlp-proto-grpc.
      Guard all imports with try/except ImportError so the app starts even if
      the OTel packages are absent (e.g. in unit tests).

O2  — Logging:
      • Configure root logger with a _DDJsonFormatter that emits JSON to stdout.
      • Every log record must include: ts (ISO-8601), level, logger, message,
        and a "dd" block with service, env, version, trace_id, span_id.
      • trace_id and span_id come from the current active OTel span context
        (only injected when a valid span is active).
      • This JSON stdout is captured by the Datadog Lambda Extension / Datadog
        Agent on Azure and GCP — no CloudWatch→Datadog integration needed.

O3  — Tracing: span hierarchy must be:
        classifier.classify_email
          └── gen_ai.<provider>.invoke     (one of the three below)
                └── (implicit: HTTP call to LLM API)
        router.dispatch
          └── router.<destination>         (slack.send, hubspot.create, etc.)

      Each span carries OTel semantic convention attributes:
        classifier.classify_email:
          cloud, model, message_id, record_id,
          classification.intent, classification.confidence,
          classification.requires_human, classification.latency_ms,
          llm.input_tokens, llm.output_tokens, llm.model_id, llm.cloud
        gen_ai spans:
          gen_ai.system, gen_ai.request.model, gen_ai.request.temperature,
          gen_ai.usage.input_tokens, gen_ai.usage.output_tokens,
          gen_ai.response.finish_reasons
        router spans:
          routing.destination, routing.channel,
          classification.intent, classification.record_id

O4  — span() context manager:
      • Yields the actual span object (not None) so callers can call
        s.set_attribute() with post-call values (token counts, finish reason).
      • Provide a _NullSpan fallback class with no-op set_attribute() /
        set_status() for when OTel is disabled (OBSERVABILITY_ENABLED=false).
      • On exception: set span status to ERROR with the exception message, then re-raise.

O5  — record_llm_call() must annotate the ACTIVE span, not create a new span.
      Call it INSIDE the with span("classifier.classify_email") block.
      Same rule for record_routing() — call it inside with span("router.dispatch").

O6  — OTLP export:
      • AWS Lambda:   OTLP gRPC → localhost:4317 (Datadog Extension receives it)
      • Azure/GCP:    OTLP gRPC → https://otlp.datadoghq.com:4317 (direct intake)
                      Set endpoint from env var OTEL_EXPORTER_OTLP_ENDPOINT.
                      When DD_API_KEY is set, attach header dd-api-key: <value>
                      via _otlp_headers() helper.
      • Also set up OTLP log exporter on the same endpoint (belt+suspenders
        alongside stdout JSON).

O7  — Each adapter (aws_bedrock.py, azure_openai.py, gcp_vertex.py) wraps its
      LLM call in a gen_ai span and sets all gen_ai.* attributes.
      Each router destination function wraps its outbound call in a router span.

O8  — Configure logging via configure_logging() called at module import of
      tracing.py. All loggers in the app inherit the JSON formatter automatically.

O9  — Control switch: OBSERVABILITY_ENABLED environment variable.
      Set to "false" in test runs (prevents OTel import requirements in CI).
      Set to "true" in all deployed environments.

──────────────────────────────────────────────────────────────────────────────
PYDANTIC v2 FIELD ORDERING RULE
──────────────────────────────────────────────────────────────────────────────
Fields used in mode="before" validators must be declared BEFORE the fields that
use them. Specifically: intent and confidence must be declared before
requires_human if requires_human's validator reads them.

──────────────────────────────────────────────────────────────────────────────
TESTS
──────────────────────────────────────────────────────────────────────────────
• Set OBSERVABILITY_ENABLED=false at the top of every test file (os.environ
  before any src imports) so OTel packages are not required.
• Mock all LLM adapters and external HTTP calls — tests must not make real API
  calls and must pass offline.
• Cover: happy path, LLM error (LLMInvocationError), JSON parse error,
  schema validation error, duplicate message_id dedup, every routing destination.
• Target ≥80% line coverage.

──────────────────────────────────────────────────────────────────────────────
OUTPUT
──────────────────────────────────────────────────────────────────────────────
Write all files to disk. Do not summarise — create the actual files.
After writing, run the test suite and report pass/fail counts.
```

---

## Prompt 2 — Terraform Infrastructure

> **Use for:** AWS, Azure, GCP, and Datadog Terraform modules.
> **Prerequisite:** Prompt 1 output (source code) must already be written.

```
You are writing production Terraform for a cross-cloud AI pipeline.
The application code already exists at src/. Your job is infra only.

──────────────────────────────────────────────────────────────────────────────
STRUCTURE
──────────────────────────────────────────────────────────────────────────────
infra/
  versions.tf            — required_providers block
  modules/
    aws/                 — main.tf, variables.tf, outputs.tf, dynamodb.tf,
                           eventbridge.tf (etc. as needed)
    azure/               — main.tf, variables.tf, outputs.tf, cosmos.tf, etc.
    gcp/                 — main.tf, variables.tf, outputs.tf, firestore.tf,
                           eventarc.tf, etc.
    datadog/             — main.tf, variables.tf, outputs.tf, dashboard.tf,
                           monitors.tf, slo.tf
  environments/
    prod/main.tf
    staging/main.tf
  .gitignore

──────────────────────────────────────────────────────────────────────────────
PROVIDER VERSIONS
──────────────────────────────────────────────────────────────────────────────
aws      = ~> 6.0
azurerm  = ~> 4.0
google   = ~> 7.0
datadog  = ~> 3.0
random   = ~> 3.6
archive  = ~> 2.4

──────────────────────────────────────────────────────────────────────────────
IMPLEMENTATION DISCIPLINE — same rules as Prompt 1; also apply:
──────────────────────────────────────────────────────────────────────────────
TD1 — SWEEP ALL RESOURCES. When a setting applies to a resource type (e.g.
      tracing_config on Lambda, https_only on Function Apps, TLS version on
      storage accounts), apply it to EVERY resource of that type — not just
      the one you're currently editing.

TD2 — ACCEPTED-RISK REGISTER. Any item you consciously choose NOT to
      implement must be recorded in `infra/SECURITY-DECISIONS.md` with:
        - Finding ID and file location
        - One-line justification (not just "low priority")
        - Owner and review date
      Also add a brief inline comment in the .tf file referencing the ID:
        # ACCEPTED RISK: T12 — see infra/SECURITY-DECISIONS.md
      This prevents the next reviewer from re-flagging the same items.
      Create SECURITY-DECISIONS.md at the start of Prompt 2 work, not the end.

──────────────────────────────────────────────────────────────────────────────
SECURITY — apply every item
──────────────────────────────────────────────────────────────────────────────
T1  — No wildcard (*) IAM actions or resources anywhere.
T2  — No secrets in any .tf, .tfvars, or source-controlled file.
      All sensitive values via TF_VAR_* env vars or CI secrets injection.
      Add *.tfvars to infra/.gitignore.
      Add a placeholder comment in each env main.tf:
        # After first `terraform init`, commit the generated .terraform.lock.hcl
        # (see MANUAL STEPS below)
T3  — AWS IAM: scope CloudWatch Logs ARNs to account_id (use
      data "aws_caller_identity" "current") — not wildcard account.
T4  — GCP: no allUsers IAM bindings. Create dedicated service accounts for
      each invoker (pubsub_invoker, feedback_invoker) and bind Cloud Run
      invoker role to those SAs. Add OIDC token creator binding for Pub/Sub.
T5  — Azure storage: shared_access_key_enabled = false,
      min_tls_version = "TLS1_2", https_traffic_only_enabled = true.
      Storage replication: ZRS for production, LRS acceptable for dev only.
      Function app: storage_uses_managed_identity = true.
      CosmosDB: local_authentication_disabled = true,
                public_network_access_enabled = false.
      Add azurerm_role_assignment for Storage Blob Data Owner for each
      Function App managed identity.
T6  — Tag all resources: project, env, cloud, owner.
T7  — GCP: never set force_destroy = true on production storage buckets or
      Firestore databases. Gate it on var.env == "dev" if needed for testing.
T8  — GCP Secret Manager: never reference version = "latest" — always pin to
      a specific version string (e.g. "1") and update deliberately.
T9  — AWS DynamoDB: enable point_in_time_recovery for ALL environments
      (not just prod). The gate var.env == "prod" is a false economy —
      you will need to restore a staging table eventually.
T10 — Every Lambda function (classifier AND feedback AND any future) must
      have an identical set of: tracing_config, layers, DD env vars, and
      timeout/memory settings. Never configure a subset of functions.
T11 — All three LLM adapters (Bedrock, Azure OpenAI, Vertex AI) must have
      identical retry behaviour: same max attempts, same jittered backoff,
      same error classification (transient vs. permanent). A missing retry
      loop on one adapter makes cross-cloud eval results non-comparable and
      degrades production SLA on that cloud asymmetrically.

──────────────────────────────────────────────────────────────────────────────
AWS MODULE — Datadog Lambda Extension
──────────────────────────────────────────────────────────────────────────────
D1  — Add a dd_api_key_secret_arn variable (default = "", meaning disabled).
      When set, attach the Datadog-Extension-ARM layer to every Lambda.
      Layer ARN: arn:aws:lambda:<region>:464622532012:layer:Datadog-Extension-ARM:<version>
      Pin the version in a dd_extension_version variable with a comment pointing
      to the GitHub releases page. Do NOT use "latest".
D2  — When DD Extension is enabled, inject these env vars into every Lambda:
        DD_SITE, DD_SERVICE, DD_ENV, DD_VERSION
        DD_LOGS_ENABLED=true          (Extension captures stdout JSON → DD Logs)
        DD_LOGS_INJECTION=true        (Injects trace correlation into log records)
        DD_TRACE_ENABLED=true
        DD_OTLP_CONFIG_RECEIVER_PROTOCOLS_GRPC_ENDPOINT=localhost:4317
        DD_API_KEY_SECRET_ARN=<secret_arn>  (Extension reads key at startup)
        OBSERVABILITY_ENABLED=true
      When disabled: OBSERVABILITY_ENABLED=false only.
D3  — Grant the Lambda execution role GetSecretValue on the DD API key secret
      (when enabled). Use compact() to drop the empty ARN when disabled.
D4  — Set tracing_config mode to "PassThrough" (not "Active") when DD Extension
      is enabled to avoid double-instrumentation cost with X-Ray.

──────────────────────────────────────────────────────────────────────────────
DATADOG MODULE — infra/modules/datadog/
──────────────────────────────────────────────────────────────────────────────
Provision:
  • provider "datadog" using dd_api_key and dd_app_key variables (sensitive).
  • Dashboard: LLM Observability + APM overview with widgets for:
      - Classification count (1h)         - Error rate timeseries
      - p50/p95 latency timeseries        - Requires-human rate query_value
      - Token usage by model/cloud (bars) - LLM span latency by cloud (p95)
      - Intent distribution toplist       - Routing destinations toplist
      - Routing latency by destination    - Lambda invocations + errors
  • Monitors:
      1. classification_error_rate  — % errored classify_email spans (default alert: 5%)
      2. classification_latency_p95 — p95 of classify_email span (default: 8000ms)
      3. human_review_rate          — % dispatched to human_queue (default: 30%)
      4. pipeline_no_data           — dead-man: 0 spans in 30m (prod only)
      5. llm_token_spike            — output tokens/s spike (warn 30, crit 50)
  • SLO: metric-based, good = hits - errors / hits, 99.5% target over 7d and 30d.
  • SLO burn-rate monitors: fast (1h, 14.4×) and slow (6h, 6×).

Query conventions (span metrics from OTel → Datadog APM):
  trace.<span_name>.hits          — span invocation count
  trace.<span_name>.errors        — errored span count
  p95:trace.<span_name>{...}      — p95 latency
  gen_ai.usage.input_tokens{...}  — token count metric (from gen_ai.* span attrs)
  gen_ai.usage.output_tokens{...}

──────────────────────────────────────────────────────────────────────────────
MANUAL STEPS (cannot be automated — list these at the end of your response)
──────────────────────────────────────────────────────────────────────────────
The following actions require running real commands and cannot be produced by
writing files. Document them as explicit instructions for the operator:

M1  — After `terraform init` in each environment directory, commit the
      generated `.terraform.lock.hcl` to source control. Without this,
      provider resolution is non-deterministic across CI runs.
      Command: cd infra/environments/<env> && terraform init && git add .terraform.lock.hcl

M2  — Generate the actual Lambda deployment package by running the build
      step before first `terraform apply`:
      Command: make build   (or equivalent zip command in the Makefile)

──────────────────────────────────────────────────────────────────────────────
OUTPUT
──────────────────────────────────────────────────────────────────────────────
Write all .tf files to disk. Do not summarise — create the actual files.
Run `terraform validate` in each module directory and report results.
List all MANUAL STEPS the operator must run after the files are written.
```

---

## Prompt 3 — Adversarial Security Review

> **Use for:** Final security audit pass after Prompts 1 and 2 are complete.
> **Run this as a separate agent / fresh context.** It should have no memory of
> the implementation conversation so it can give a genuinely independent review.

```
You are a hostile security reviewer. Your job is to find every exploitable
flaw, dangerous default, and silent failure mode in the code and infrastructure
below. You are NOT trying to be helpful — you are trying to break this system.

For each finding output a structured entry:

  ID: <sequential number>
  Severity: CRITICAL | HIGH | MEDIUM | LOW | INFO
  Location: <file:line or module>
  Title: <short description>
  Detail: <what is wrong and why it matters>
  Fix: <minimal concrete fix>

Review the following categories without mercy:

SECRETS & CREDENTIALS
  - Any secret, key, token, or credential in source code, log output,
    error messages, or Terraform state.
  - Credentials passed as function arguments that could end up in stack traces.
  - Log statements that include request/response bodies, headers, or env vars.

INPUT VALIDATION & INJECTION
  - LLM prompt injection: can attacker-controlled email content manipulate the
    system prompt or override the classification output?
  - Schema validation: are all external inputs (webhook payloads, LLM responses,
    SQS/Pub/Sub messages) validated before use?
  - Path traversal, SSRF, or command injection in any file/URL handling.

IAM & LEAST PRIVILEGE
  - Wildcard (*) actions or resources in any IAM policy, service account binding,
    or Azure role assignment.
  - Over-broad CloudWatch Logs ARNs (account-level wildcard instead of function ARN).
  - allUsers or unauthenticated public access on any GCP Cloud Run / Cloud Function.
  - Azure storage access keys enabled when managed identity should be used.
  - Missing authorization on webhook endpoints (anyone can POST?).

LLM-SPECIFIC
  - MAX_TOKENS finish reason silently returning truncated JSON.
  - temperature != 0 allowing non-deterministic outputs.
  - Missing retry / back-off for rate-limit errors.
  - No timeout on LLM API calls.
  - LLM response parsed without schema validation.

PYTHON CODE QUALITY
  - datetime.utcnow() usage (timezone-naive — always a bug).
  - Bare except: clauses swallowing errors.
  - Pydantic model_construct() bypassing validation.
  - Mutable default arguments.
  - Missing __all__ on public modules.

OBSERVABILITY RISKS
  - Spans or logs created outside the correct parent span context.
  - record_llm_call() or record_routing() called after the span context exits
    (creates orphan/sibling spans that never appear in the trace).
  - Log records containing raw LLM output (attacker-controlled data in logs).
  - OTLP exporter construction not wrapped in try/except — crash on cold start
    if the collector endpoint is unreachable.

RUNTIME CONFIGURATION
  - Required env vars validated at first request rather than at module import.
    (A misconfigured deploy should fail immediately, not on the first prod request.)
  - In-memory state (sets, dicts, caches) with no eviction bound — memory leak
    under steady traffic.

PAIRED CONTROL GAPS (new controls that introduce new problems)
  - Retry logic without jitter — thundering-herd under rate-limit storms when
    multiple instances back off at exactly the same time.
  - Retry logic on non-idempotent endpoints without idempotency keys — retries
    can produce duplicate side effects (duplicate Slack messages, Linear issues, etc.)
  - Auth mitigation (HMAC, schema delimiters) without a corresponding test that
    proves the mitigation holds against a malicious/invalid input.
  - Dynamic __import__() calls that break on module rename.

TERRAFORM / INFRASTRUCTURE
  - Any .tfvars file that could contain secrets checked into source control.
  - Missing .gitignore entries for state files, .tfvars, .terraform/ directory.
  - Missing .terraform.lock.hcl in any environment directory.
  - Provider version constraints too loose (e.g. version = ">= 1.0" with no upper bound).
  - CosmosDB local authentication not disabled.
  - CosmosDB / Azure storage public network access not explicitly disabled.
  - Azure storage: missing min_tls_version or https_traffic_only_enabled.
  - Storage shared access keys enabled.
  - GCP secret version pinned to "latest" instead of a specific version number.
  - GCP force_destroy = true on non-dev resources.
  - DynamoDB PITR disabled or gated on prod-only literal string comparison.
  - Any Lambda function missing tracing_config, layers, or DD env vars that
    other Lambda functions in the same module have.
  - Missing OTLP endpoint / DD env vars when observability is expected.

After listing all findings, produce:

1. SUMMARY table: Total | Critical | High | Medium | Low | Info

2. TRIAGE TABLE — every finding must be in exactly one category:
   MUST FIX    — blocks production rollout (security, data loss, availability)
   SHOULD FIX  — significant tech debt; fix in next iteration
   ACCEPTED RISK — consciously deferred; document owner and review date

   Any finding not in ACCEPTED RISK must appear in the fix list.
   ACCEPTED RISK items must have explicit justification — "low priority" is
   not a justification.

3. PRIORITIZED FIX LIST — the top 10 MUST FIX + SHOULD FIX items in order
   of risk × effort, with a one-line description of each fix.

4. PAIRED CONTROL AUDIT — for every new control added since the last review
   (retry logic, auth, validation, observability), state:
   - What new attack surface or failure mode does this control introduce?
   - What paired control is required to close it?

Source to review: [attach the full src/ and infra/ directory contents]
```

---

## Usage Notes

**One-shot approach:** Run Prompt 1 → Prompt 2 → Prompt 3 in sequence in a single
session. Prompts 1 and 2 are generative (write files). Prompt 3 is evaluative
(find problems). Apply Prompt 3 fixes immediately in the same session.

**Chain approach (lower context cost):** Run each prompt in a fresh session.
This keeps each context window small, which reduces token cost and improves
model attention. Prompt 3 should always be a fresh context so it has no anchoring
bias from the implementation conversation.

**Why fresh context for Prompt 3 matters:** A model that wrote the code will
rationalize its own decisions under review. A fresh context has no sunk cost
and will flag the same issues a hostile external reviewer would. This is the
single most important process rule in this template.

**Why findings recur across review passes:** Four root causes were observed
building this project:
1. "Sweep all instances" wasn't explicit — model fixed the first example found
   and stopped. (Now addressed by D1/TD1.)
2. Paired controls weren't required — retry added without jitter/idempotency;
   auth added without a test proving it. (Now addressed by D2 and Paired Control
   Audit in Prompt 3.)
3. New code wasn't re-reviewed — OTel rewrite introduced N2/N9/N10. Always
   treat significant new modules as a re-review trigger. (See trigger list below.)
4. Terraform mediums were silently deferred without documentation — T11–T22
   were all skipped. Now addressed by TD2 (accepted-risk register) and the
   expanded T5–T10 requirements.

**When to re-run Prompt 3:**
- After any significant schema or routing change.
- After adding any new control (retry, auth, validation, OTel) — new code
  introduces new failure modes; the old review doesn't cover it.
- After upgrading provider/library versions.
- Before shipping to production: always run from a clean context.

**After running Prompt 3 fixes:**
- Every finding must end up in MUST FIX (fixed this pass), SHOULD FIX
  (tracked for next iteration), or ACCEPTED RISK (documented with justification).
  An unfixed finding with no documented disposition is a future re-finding.

**Manual steps that cannot be automated (do these after every Prompt 2 run):**
1. `cd infra/environments/<env> && terraform init && git add .terraform.lock.hcl`
   — must be done for every environment to produce deterministic provider resolution.
2. Run `make build` (or equivalent) to produce the Lambda deployment package
   before `terraform apply`.

**Variables to fill in before using:**
  - `<your-project-name>` — project identifier used in resource names
  - `<one-sentence description>` — what the pipeline classifies/routes
  - `<your GitHub username>` — controls repo push destination guard
  - Routing destinations (the destinations.py handlers) — list what systems
    the classified emails are routed to (Slack, HubSpot, Linear, email forward, etc.)
  - LLM model IDs if different from Claude Haiku / GPT-4o-mini / Gemini Flash
