# ── Cosmos DB (NoSQL API, Serverless) ────────────────────────────────────────

resource "azurerm_cosmosdb_account" "main" {
  name                = "cosmos-${local.name_short}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  offer_type          = "Standard"
  kind                = "GlobalDocumentDB"
  # Disable key-based auth — all access via managed identity / RBAC (T7 consistency)
  local_authentication_disabled = true
  tags                          = local.common_tags

  capabilities {
    name = "EnableServerless"
  }

  consistency_policy {
    consistency_level = "Session"
  }

  geo_location {
    location          = azurerm_resource_group.main.location
    failover_priority = 0
  }
}

resource "azurerm_cosmosdb_sql_database" "main" {
  name                = "inbox-triage"
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
}

resource "azurerm_cosmosdb_sql_container" "classifications" {
  name                = "classifications"
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
  database_name       = azurerm_cosmosdb_sql_database.main.name
  partition_key_paths = ["/record_id"]

  # TTL — 90 days default, overridden per-document by Lambda
  default_ttl = 7776000

  indexing_policy {
    indexing_mode = "consistent"

    included_path { path = "/*" }

    # GSI equivalent — query by intent
    composite_index {
      index {
        path  = "/intent"
        order = "Ascending"
      }
      index {
        path  = "/classified_at"
        order = "Descending"
      }
    }
  }
}

resource "azurerm_cosmosdb_sql_container" "feedback" {
  name                = "feedback"
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
  database_name       = azurerm_cosmosdb_sql_database.main.name
  partition_key_paths = ["/record_id"]
  default_ttl         = 31536000  # 1 year
}
