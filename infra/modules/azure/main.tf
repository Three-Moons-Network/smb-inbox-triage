locals {
  # Azure resource names must be short and alphanumeric in many cases
  name_prefix  = "${var.project_name}-${var.env}"
  name_short   = "inboxtriage${var.env}"  # used for storage, key vault (<=24 chars)
  common_tags  = merge(
    {
      project = var.project_name
      env     = var.env
      cloud   = "azure"
      owner   = "platform-team"
    },
    var.tags,
  )

  # ── Datadog OTLP direct-intake observability env vars ──────────────────────
  #
  # Flex Consumption Python functions have no sidecar/extension absorber, so
  # OTLP is sent directly to Datadog's intake. PYTHON_ENABLE_OPENTELEMETRY is
  # MANDATORY — without it the Functions host suppresses worker-side OTel
  # output and nothing arrives at Datadog (no error, no log). Direct intake
  # for traces is still Preview at Datadog (May 2026), so this is the path of
  # last resort on Azure; if intake remains spotty, move this Function App
  # to Azure Container Apps with a real Datadog Agent sidecar.
  dd_observability_settings = var.dd_enabled ? {
    OBSERVABILITY_ENABLED             = "true"
    PYTHON_ENABLE_OPENTELEMETRY       = "true"
    DD_SITE                           = var.dd_site
    DD_SERVICE                        = var.dd_service
    DD_ENV                            = var.env
    DD_VERSION                        = var.dd_version
    DD_LOGS_INJECTION                 = "true"
    # OTel SDK targets — direct HTTPS to Datadog OTLP intake
    OTEL_EXPORTER_OTLP_ENDPOINT       = "https://otlp.${var.dd_site}"
    OTEL_EXPORTER_OTLP_HEADERS        = "DD-API-KEY=@Microsoft.KeyVault(SecretUri=${azurerm_key_vault.main.vault_uri}secrets/${var.dd_api_key_secret_name}/)"
    OTEL_EXPORTER_OTLP_PROTOCOL       = "http/protobuf"
    OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE = "delta"
    OTEL_BSP_SCHEDULE_DELAY           = "500"
    # DD_API_KEY is also exposed for libraries that read it directly (dd-trace, etc.)
    DD_API_KEY                        = "@Microsoft.KeyVault(SecretUri=${azurerm_key_vault.main.vault_uri}secrets/${var.dd_api_key_secret_name}/)"
  } : {
    OBSERVABILITY_ENABLED = "false"
  }
}

provider "azurerm" {
  features {
    key_vault {
      purge_soft_delete_on_destroy    = true
      recover_soft_deleted_key_vaults = true
    }
    # Azure auto-creates an "Application Insights Smart Detection" action
    # group inside the resource group whenever App Insights is provisioned.
    # That action group is NOT managed by Terraform, so the provider's
    # default safety check refuses to destroy the RG ("still contains
    # Resources"). Disable the check so `terraform destroy` can blow the
    # whole RG away in one pass via the Azure API.
    resource_group {
      prevent_deletion_if_contains_resources = false
    }
  }
}

data "azurerm_client_config" "current" {}

# ── Resource Group ────────────────────────────────────────────────────────────

resource "azurerm_resource_group" "main" {
  name     = "rg-${local.name_prefix}"
  location = var.region
  tags     = local.common_tags
}

# ── Storage Account ───────────────────────────────────────────────────────────
# Note: shared_access_key_enabled = false is deferred (T14) — the azurerm provider
# requires key-based auth internally to manage Function App storage (queue properties).
# Re-enable when provider support for full Azure AD storage auth matures.

resource "azurerm_storage_account" "main" {
  name                     = substr(replace(local.name_short, "-", ""), 0, 24)
  resource_group_name      = azurerm_resource_group.main.name
  location                 = azurerm_resource_group.main.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  tags                     = local.common_tags
}

# ── Azure Container Registry — holds the Azure Functions custom container ────
#
# Built and pushed via:
#   az acr login --name <name>
#   docker build -f Dockerfile.azure -t <name>.azurecr.io/smb-inbox-triage:latest .
#   docker push <name>.azurecr.io/smb-inbox-triage:latest
#
# The same image runs both classifier and feedback container apps — the
# Azure Functions host inside the image routes by @app.route decorator.

resource "azurerm_container_registry" "main" {
  name                = "acrinboxtriage${var.env}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  sku                 = "Basic"
  admin_enabled       = false   # use managed-identity pulls, not admin creds
  tags                = local.common_tags
}

# ── User-assigned managed identity shared by both Container Apps ──────────────
#
# Why a UAMI instead of per-app System-Assigned identities:
#   Container Apps with System-Assigned identity hit a chicken-and-egg problem
#   on first deploy: the Container App needs AcrPull to pull the image, but
#   the AcrPull role assignment can only be created after the Container App
#   exists (since the role binds to the system identity's principal ID). The
#   first revision then fails with "ACR token exchange returned 401" while
#   waiting for the role assignment to land.
#
#   A UAMI is created up-front, so AcrPull, Key Vault access, and Cosmos RBAC
#   are all in place before the Container Apps reference the identity. First
#   pull succeeds immediately.

resource "azurerm_user_assigned_identity" "app" {
  name                = "uami-${local.name_prefix}"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  tags                = local.common_tags
}

resource "azurerm_role_assignment" "uami_acr_pull" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# ── Key Vault ─────────────────────────────────────────────────────────────────

resource "azurerm_key_vault" "main" {
  name                = "kv-${local.name_short}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tenant_id           = data.azurerm_client_config.current.tenant_id
  sku_name            = "standard"
  tags                = local.common_tags

  access_policy {
    tenant_id = data.azurerm_client_config.current.tenant_id
    object_id = data.azurerm_client_config.current.object_id
    secret_permissions = ["Get", "Set", "List", "Delete", "Purge"]
  }
}

# Grant the shared UAMI read access to Key Vault secrets — used by both
# Container Apps (the dd-api-key secret reference + any future KV refs).
resource "azurerm_key_vault_access_policy" "uami_secrets" {
  key_vault_id       = azurerm_key_vault.main.id
  tenant_id          = data.azurerm_client_config.current.tenant_id
  object_id          = azurerm_user_assigned_identity.app.principal_id
  secret_permissions = ["Get"]
}

# ── Application Insights ──────────────────────────────────────────────────────

resource "azurerm_application_insights" "main" {
  name                = "ai-${local.name_prefix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  application_type    = "web"
  tags                = local.common_tags
}

# ── Log Analytics + Container App Environment ─────────────────────────────────
#
# Container Apps requires a Log Analytics workspace bound to its environment.
# Both Container Apps (classifier and feedback) share one environment so the
# image and sidecar config can be promoted uniformly.

resource "azurerm_log_analytics_workspace" "main" {
  name                = "law-${local.name_prefix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = local.common_tags
}

resource "azurerm_container_app_environment" "main" {
  name                       = "cae-${local.name_prefix}"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  tags                       = local.common_tags
}

# ── Container App — Classifier ────────────────────────────────────────────────
#
# Topology (matches AWS Lambda Extension + GCP Cloud Run sidecar pattern):
#
#   App container (Azure Functions Python image)
#       │  OTLP/HTTP  http://localhost:4318
#       ▼
#   Datadog Agent sidecar (shared network namespace within the Container App)
#       │  HTTPS/443  → Datadog intake
#       ▼
#   Datadog APM + LLM Observability + Logs
#
# The app container does NOT hold DD_API_KEY — only the sidecar does.

resource "azurerm_container_app" "classifier" {
  name                         = "ca-${local.name_prefix}-clf"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"
  tags                         = local.common_tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.app.id
  }

  # Datadog API key sourced from Key Vault via the UAMI.
  # DD API key passed through as a Container App secret value to avoid the
  # KV/UAMI initialization-order race that fails first-time provisioning.
  # Sourced from TF_VAR_dd_api_key.
  secret {
    name  = "dd-api-key"
    value = var.dd_api_key
  }

  depends_on = [
    azurerm_role_assignment.uami_acr_pull,
    azurerm_key_vault_access_policy.uami_secrets,
  ]

  template {
    min_replicas = 0
    max_replicas = 10

    # ── App container — Azure Functions custom container image ──────────────
    container {
      name   = "classifier"
      image  = "${azurerm_container_registry.main.login_server}/smb-inbox-triage:latest"
      cpu    = 0.5
      memory = "1Gi"

      env {
        name  = "CLOUD"
        value = "azure"
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.app.client_id
      }
      # FUNCTION_TARGET selects which @app.route handler the shared
      # function_app.py shim imports — see src/function_app.py.
      env {
        name  = "FUNCTION_TARGET"
        value = "classifier"
      }
      env {
        name  = "AzureWebJobsFeatureFlags"
        value = "EnableWorkerIndexing"
      }
      # On custom-container Container Apps these are NOT auto-injected by the
      # Functions host the way they are in Flex Consumption. Without them the
      # host comes up but reports "0 functions found (Custom)" and never
      # discovers any @app.route handlers in our Python v2 code.
      env {
        name  = "FUNCTIONS_WORKER_RUNTIME"
        value = "python"
      }
      env {
        name  = "FUNCTIONS_EXTENSION_VERSION"
        value = "~4"
      }
      # AzureWebJobsStorage — required by the Functions host even for HTTP-only
      # functions; missing it produces "Unable to create client for AzureWebJobsStorage"
      # health-check warnings. Reusing the existing storage account.
      env {
        name  = "AzureWebJobsStorage"
        value = azurerm_storage_account.main.primary_connection_string
      }
      # PYTHON_ENABLE_OPENTELEMETRY=false avoids a known Azure-Functions Python
      # worker crash: when enabled, the worker tries to extract trace context
      # using a global text-map propagator that we don't register, raising
      # `AttributeError: 'NoneType' object has no attribute 'extract'`. Our
      # observability/tracing.py wires up the OTel SDK directly so we don't
      # need the worker's built-in OTel integration. Re-enable after wiring
      # set_global_textmap(TraceContextTextMapPropagator()) in tracing.py.
      env {
        name  = "PYTHON_ENABLE_OPENTELEMETRY"
        value = "false"
      }
      env {
        name  = "AZURE_OPENAI_ENDPOINT"
        value = var.azure_openai_endpoint
      }
      env {
        name  = "AZURE_OPENAI_DEPLOYMENT"
        value = var.llm_model_id
      }
      env {
        name  = "AZURE_OPENAI_API_VERSION"
        value = "2024-08-01-preview"
      }
      # IMPORTANT: Container Apps does NOT resolve the
      # @Microsoft.KeyVault(SecretUri=...) syntax the way Function Apps do.
      # Pass the resolved key literally via TF_VAR_azure_openai_api_key.
      env {
        name  = "AZURE_OPENAI_API_KEY"
        value = var.azure_openai_api_key
      }
      # SLACK_WEBHOOK_URL left as a placeholder — Slack notification is
      # wrapped in try/except in router/destinations.py so a missing/invalid
      # value only suppresses notifications, doesn't fail classification.
      # If you need real Slack delivery, follow the same TF_VAR pattern as
      # azure_openai_api_key above.
      env {
        name  = "SLACK_WEBHOOK_URL"
        value = ""
      }
      env {
        name        = "COSMOS_CONNECTION_STRING"
        value = azurerm_cosmosdb_account.main.endpoint
      }
      env {
        name        = "COSMOS_DATABASE"
        value = azurerm_cosmosdb_sql_database.main.name
      }
      env {
        name        = "COSMOS_CONTAINER_CLASSIFICATIONS"
        value = azurerm_cosmosdb_sql_container.classifications.name
      }
      env {
        name        = "OBSERVABILITY_ENABLED"
        value = "true"
      }
      env {
        name        = "DD_SERVICE"
        value = var.dd_service
      }
      env {
        name        = "DD_ENV"
        value = var.env
      }
      env {
        name        = "DD_VERSION"
        value = var.dd_version
      }
      env {
        name        = "DD_SITE"
        value = var.dd_site
      }
      env {
        name        = "OTEL_EXPORTER_OTLP_ENDPOINT"
        value = "http://localhost:4318"
      }
      env {
        name        = "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"
        value = "delta"
      }
      env {
        name        = "OTEL_BSP_SCHEDULE_DELAY"
        value = "500"
      }
      env {
        name        = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = azurerm_application_insights.main.connection_string
      }
    }

    # ── Datadog Agent sidecar — absorber on localhost:4318 ──────────────────
    container {
      name   = "datadog-agent"
      image  = "gcr.io/datadoghq/agent:7"
      cpu    = 0.5
      memory = "1Gi"

      env {
        name        = "DD_SITE"
        value = var.dd_site
      }
      env {
        name        = "DD_API_KEY"
        secret_name = "dd-api-key"
      }
      env {
        name        = "DD_OTLP_CONFIG_RECEIVER_PROTOCOLS_HTTP_ENDPOINT"
        value = "0.0.0.0:4318"
      }
      env {
        name        = "DD_APM_OTLP_ENABLED"
        value = "true"
      }
      env {
        name        = "DD_OTLP_CONFIG_TRACES_ENABLED"
        value = "true"
      }
      env {
        name        = "DD_OTLP_CONFIG_METRICS_ENABLED"
        value = "true"
      }
      env {
        name        = "DD_OTLP_CONFIG_LOGS_ENABLED"
        value = "true"
      }
      env {
        name        = "DD_SERVICE"
        value = var.dd_service
      }
      env {
        name        = "DD_ENV"
        value = var.env
      }
      env {
        name        = "DD_VERSION"
        value = var.dd_version
      }
      env {
        name  = "DD_HOSTNAME"
        value = "${local.name_prefix}-classifier"
      }
      env {
        name        = "DD_APM_ENABLED"
        value = "true"
      }
      env {
        name        = "DD_LOGS_ENABLED"
        value = "true"
      }
      env {
        name        = "DD_PROCESS_AGENT_ENABLED"
        value = "false"
      }
      env {
        name        = "DD_SYSTEM_PROBE_ENABLED"
        value = "false"
      }
      env {
        name        = "DD_DOGSTATSD_NON_LOCAL_TRAFFIC"
        value = "false"
      }
    }
  }

  ingress {
    external_enabled = true
    target_port      = 80    # Azure Functions Python container listens on 80
    transport        = "auto"
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
}

# ── Container App — Feedback ──────────────────────────────────────────────────

resource "azurerm_container_app" "feedback" {
  name                         = "ca-${local.name_prefix}-fb"
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"
  tags                         = local.common_tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.app.id
  }

  # DD API key passed through as a Container App secret value to avoid the
  # KV/UAMI initialization-order race that fails first-time provisioning.
  # Sourced from TF_VAR_dd_api_key.
  secret {
    name  = "dd-api-key"
    value = var.dd_api_key
  }

  depends_on = [
    azurerm_role_assignment.uami_acr_pull,
    azurerm_key_vault_access_policy.uami_secrets,
  ]

  template {
    min_replicas = 0
    max_replicas = 5

    container {
      name   = "feedback"
      image  = "${azurerm_container_registry.main.login_server}/smb-inbox-triage:latest"
      cpu    = 0.5
      memory = "1Gi"

      env {
        name  = "CLOUD"
        value = "azure"
      }
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.app.client_id
      }
      env {
        name  = "FUNCTION_TARGET"
        value = "feedback"
      }
      env {
        name  = "AzureWebJobsFeatureFlags"
        value = "EnableWorkerIndexing"
      }
      env {
        name  = "FUNCTIONS_WORKER_RUNTIME"
        value = "python"
      }
      env {
        name  = "FUNCTIONS_EXTENSION_VERSION"
        value = "~4"
      }
      env {
        name  = "AzureWebJobsStorage"
        value = azurerm_storage_account.main.primary_connection_string
      }
      # See classifier block for why this is false.
      env {
        name  = "PYTHON_ENABLE_OPENTELEMETRY"
        value = "false"
      }
      env {
        name        = "COSMOS_CONNECTION_STRING"
        value = azurerm_cosmosdb_account.main.endpoint
      }
      env {
        name        = "COSMOS_DATABASE"
        value = azurerm_cosmosdb_sql_database.main.name
      }
      env {
        name        = "COSMOS_CONTAINER_CLASSIFICATIONS"
        value = azurerm_cosmosdb_sql_container.classifications.name
      }
      env {
        name        = "OBSERVABILITY_ENABLED"
        value = "true"
      }
      env {
        name        = "DD_SERVICE"
        value = var.dd_service
      }
      env {
        name        = "DD_ENV"
        value = var.env
      }
      env {
        name        = "DD_VERSION"
        value = var.dd_version
      }
      env {
        name        = "DD_SITE"
        value = var.dd_site
      }
      env {
        name        = "OTEL_EXPORTER_OTLP_ENDPOINT"
        value = "http://localhost:4318"
      }
      env {
        name        = "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"
        value = "delta"
      }
      env {
        name        = "OTEL_BSP_SCHEDULE_DELAY"
        value = "500"
      }
      env {
        name        = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = azurerm_application_insights.main.connection_string
      }
    }

    container {
      name   = "datadog-agent"
      image  = "gcr.io/datadoghq/agent:7"
      cpu    = 0.5
      memory = "1Gi"

      env {
        name        = "DD_SITE"
        value = var.dd_site
      }
      env {
        name        = "DD_API_KEY"
        secret_name = "dd-api-key"
      }
      env {
        name        = "DD_OTLP_CONFIG_RECEIVER_PROTOCOLS_HTTP_ENDPOINT"
        value = "0.0.0.0:4318"
      }
      env {
        name        = "DD_APM_OTLP_ENABLED"
        value = "true"
      }
      env {
        name        = "DD_OTLP_CONFIG_TRACES_ENABLED"
        value = "true"
      }
      env {
        name        = "DD_OTLP_CONFIG_METRICS_ENABLED"
        value = "true"
      }
      env {
        name        = "DD_OTLP_CONFIG_LOGS_ENABLED"
        value = "true"
      }
      env {
        name        = "DD_SERVICE"
        value = var.dd_service
      }
      env {
        name        = "DD_ENV"
        value = var.env
      }
      env {
        name        = "DD_VERSION"
        value = var.dd_version
      }
      env {
        name  = "DD_HOSTNAME"
        value = "${local.name_prefix}-feedback"
      }
      env {
        name        = "DD_APM_ENABLED"
        value = "true"
      }
      env {
        name        = "DD_LOGS_ENABLED"
        value = "true"
      }
      env {
        name        = "DD_PROCESS_AGENT_ENABLED"
        value = "false"
      }
      env {
        name        = "DD_SYSTEM_PROBE_ENABLED"
        value = "false"
      }
      env {
        name        = "DD_DOGSTATSD_NON_LOCAL_TRAFFIC"
        value = "false"
      }
    }
  }

  ingress {
    external_enabled = true
    target_port      = 80
    transport        = "auto"
    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }
}

# ── Cosmos DB RBAC binding for the shared UAMI ────────────────────────────────
#
# Both Container Apps connect to Cosmos via DefaultAzureCredential. Because
# they share one UAMI, one role binding is enough.

data "azurerm_cosmosdb_sql_role_definition" "data_contributor" {
  account_name        = azurerm_cosmosdb_account.main.name
  resource_group_name = azurerm_resource_group.main.name
  role_definition_id  = "00000000-0000-0000-0000-000000000002"  # Data Contributor
}

resource "azurerm_cosmosdb_sql_role_assignment" "uami_data" {
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
  role_definition_id  = data.azurerm_cosmosdb_sql_role_definition.data_contributor.id
  principal_id        = azurerm_user_assigned_identity.app.principal_id
  scope               = azurerm_cosmosdb_account.main.id
}
