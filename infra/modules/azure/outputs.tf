output "webhook_url" {
  description = "POST inbound email payloads here."
  value       = "https://${azurerm_container_app.classifier.latest_revision_fqdn}/api/webhook"
}

output "feedback_url" {
  description = "POST human corrections here."
  value       = "https://${azurerm_container_app.feedback.latest_revision_fqdn}/api/feedback"
}

output "acr_login_server" {
  description = "Azure Container Registry login server — `docker push` target."
  value       = azurerm_container_registry.main.login_server
}

output "acr_name" {
  description = "Azure Container Registry name — for `az acr login --name`."
  value       = azurerm_container_registry.main.name
}

output "datastore_name" {
  description = "Cosmos DB classifications container."
  value       = azurerm_cosmosdb_sql_container.classifications.name
}

output "cosmos_account_endpoint" {
  value = azurerm_cosmosdb_account.main.endpoint
}

output "app_insights_instrumentation_key" {
  value     = azurerm_application_insights.main.instrumentation_key
  sensitive = true
}
