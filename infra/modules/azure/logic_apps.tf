# ── Logic Apps Consumption — routing workflow ─────────────────────────────────
# Logic Apps Consumption (multi-tenant) needs no dedicated App Service Plan.
# The workflow definition (JSON) is managed separately in
# src/logic_apps_workflows/ and deployed via the Azure CLI or az rest.
#
# Note: switched from azurerm_logic_app_standard (requires WS1 Workflow Standard
# plan, ~$185/month) to azurerm_logic_app_workflow (true pay-per-execution, ~$0
# at dev volumes). Upgrade to Standard before production if stateful workflows
# or VNet integration are required.
#
# App Insights integration: configure a Diagnostic Setting on this resource to
# forward run history to the ai-${local.name_prefix} workspace.
# Slack webhook URL: reference Key Vault from within the workflow definition
# using the Key Vault connector action; the Managed Identity below grants access.

resource "azurerm_logic_app_workflow" "router" {
  name                = "logic-${local.name_prefix}-router"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  tags                = local.common_tags

  identity {
    type = "SystemAssigned"
  }
}

# Grant the Logic App's Managed Identity Get access to Key Vault secrets
# (used to resolve Slack webhook URL inside the workflow definition).
resource "azurerm_key_vault_access_policy" "logic_app" {
  key_vault_id = azurerm_key_vault.main.id
  tenant_id    = data.azurerm_client_config.current.tenant_id
  object_id    = azurerm_logic_app_workflow.router.identity[0].principal_id
  secret_permissions = ["Get"]
}
