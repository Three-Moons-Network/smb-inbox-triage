#!/usr/bin/env bash
#
# load-env.sh — single source of truth for bash env vars used by the eval
# targets in the Makefile (eval-bedrock, eval-azure, eval-gcp, eval-compare).
#
# Sourced, not executed:
#     . scripts/load-env.sh
#
# What this does:
#   1. Reads the project .env (the same file profile.ps1 loads on PowerShell
#      start) and exports each KEY=VALUE, mapping hyphens in KEY to underscores
#      so e.g. "DD-API-KEY" → $DD_API_KEY. The .env is the source of truth for
#      AWS access keys and Datadog secrets.
#   2. Sets defaults for the model IDs, regions, and GCP project that match
#      what the deployed Terraform uses (kept in sync with the .tfvars files).
#   3. If AZURE_OPENAI_API_KEY isn't already set, pulls it from Key Vault via
#      `az keyvault secret show`. This needs `az login` to have happened.
#
# Anything you set in the shell before sourcing wins — defaults only fill
# blanks. Override per-call with e.g.:
#     BEDROCK_MODEL_ID=anthropic.claude-3-haiku-20240307-v1:0 make eval-bedrock

# ── Locate repo root regardless of where the script is invoked from ──────────
__LOAD_ENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"

# ── 1. Load .env, mapping hyphens to underscores in keys ─────────────────────
if [[ -f "$__LOAD_ENV_DIR/.env" ]]; then
    while IFS= read -r __line || [[ -n "$__line" ]]; do
        __line="${__line%$'\r'}"             # strip CR if Windows line-endings
        [[ -z "$__line" || "$__line" =~ ^[[:space:]]*# ]] && continue
        if [[ "$__line" =~ ^[[:space:]]*([A-Za-z0-9_-]+)=(.*)$ ]]; then
            __key="${BASH_REMATCH[1]//-/_}"  # hyphens → underscores
            __val="${BASH_REMATCH[2]}"
            # Strip surrounding quotes
            if [[ "$__val" =~ ^\".*\"$ || "$__val" =~ ^\'.*\'$ ]]; then
                __val="${__val:1:-1}"
            fi
            export "$__key=$__val"
        fi
    done < "$__LOAD_ENV_DIR/.env"
    unset __key __val __line
fi

# ── 2. AWS Bedrock defaults (kept in sync with infra/environments/aws-dev/terraform.tfvars) ──
export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-$AWS_REGION}"
export BEDROCK_MODEL_ID="${BEDROCK_MODEL_ID:-us.anthropic.claude-haiku-4-5-20251001-v1:0}"

# ── 3. GCP Vertex defaults (kept in sync with infra/environments/gcp-dev/terraform.tfvars) ──
export GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT:-your-gcp-project-id}"
export VERTEX_AI_LOCATION="${VERTEX_AI_LOCATION:-us-central1}"
export VERTEX_MODEL_ID="${VERTEX_MODEL_ID:-gemini-2.5-flash}"

# Vertex SDK reads Application Default Credentials. From WSL, the quickest
# path is:  gcloud auth application-default login
# (Skip if you already have a service-account key at $GOOGLE_APPLICATION_CREDENTIALS.)

# ── 4. Azure OpenAI (endpoint is static; key pulled live from KV) ────────────
export AZURE_OPENAI_ENDPOINT="${AZURE_OPENAI_ENDPOINT:-https://your-openai-resource.openai.azure.com/}"
export AZURE_OPENAI_DEPLOYMENT="${AZURE_OPENAI_DEPLOYMENT:-gpt-4.1-mini}"
export AZURE_OPENAI_API_VERSION="${AZURE_OPENAI_API_VERSION:-2024-08-01-preview}"

if [[ -z "${AZURE_OPENAI_API_KEY:-}" ]] && command -v az >/dev/null 2>&1; then
    # On WSL, `az` often resolves to az.exe (Windows binary), which emits CRLF
    # line endings. Shell command substitution only strips trailing \n, not \r,
    # so the captured key ends up as "value\r" — which then fails OpenAI auth
    # with `httpx.LocalProtocolError: Illegal header value`. `tr -d '\r\n'`
    # strips both. Also strip trailing/leading whitespace defensively.
    __aoai_key="$(az keyvault secret show \
        --vault-name your-key-vault-name \
        --name azure-openai-key \
        --query value -o tsv 2>/dev/null | tr -d '\r\n' || true)"
    if [[ -n "$__aoai_key" ]]; then
        export AZURE_OPENAI_API_KEY="$__aoai_key"
    fi
    unset __aoai_key
fi

# ── 5. Defensive: strip CR from sensitive values (handles pasted-in CR) ──────
__strip_cr() { printf '%s' "$1" | tr -d '\r\n'; }
[[ -n "${AZURE_OPENAI_API_KEY:-}" ]] && export AZURE_OPENAI_API_KEY="$(__strip_cr "$AZURE_OPENAI_API_KEY")"
[[ -n "${AWS_ACCESS_KEY_ID:-}"     ]] && export AWS_ACCESS_KEY_ID="$(__strip_cr "$AWS_ACCESS_KEY_ID")"
[[ -n "${AWS_SECRET_ACCESS_KEY:-}" ]] && export AWS_SECRET_ACCESS_KEY="$(__strip_cr "$AWS_SECRET_ACCESS_KEY")"
[[ -n "${DD_API_KEY:-}"            ]] && export DD_API_KEY="$(__strip_cr "$DD_API_KEY")"
[[ -n "${DD_APP_KEY:-}"            ]] && export DD_APP_KEY="$(__strip_cr "$DD_APP_KEY")"
unset -f __strip_cr

# ── 6. Datadog name aliases ──────────────────────────────────────────────────
[[ -n "${DD_API_KEY:-}" ]] && export DATADOG_API_KEY="${DATADOG_API_KEY:-$DD_API_KEY}"
[[ -n "${DD_APP_KEY:-}" ]] && export DATADOG_APP_KEY="${DATADOG_APP_KEY:-$DD_APP_KEY}"
