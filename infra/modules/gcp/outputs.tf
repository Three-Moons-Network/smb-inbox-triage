output "webhook_url" {
  description = "POST inbound email payloads here."
  value       = "${google_cloud_run_v2_service.classifier.uri}/webhook"
}

output "feedback_url" {
  description = "POST human corrections here."
  value       = "${google_cloud_run_v2_service.feedback.uri}/feedback"
}

output "classifier_service_name" {
  description = "Cloud Run V2 service name — pass to `gcloud run services` commands."
  value       = google_cloud_run_v2_service.classifier.name
}

output "feedback_service_name" {
  value = google_cloud_run_v2_service.feedback.name
}

output "artifact_registry_repo" {
  description = "Artifact Registry repo — push container images here."
  value       = google_artifact_registry_repository.functions.id
}

output "datastore_name" {
  description = "Firestore database name."
  value       = google_firestore_database.main.name
}

output "gmail_pubsub_topic" {
  description = "Configure Gmail Watch API to push to this topic."
  value       = google_pubsub_topic.gmail_inbound.id
}

output "classifier_sa_email" {
  value = google_service_account.classifier.email
}

# T1/T2: expose invoker SA emails so callers can configure Pub/Sub push subscriptions
output "pubsub_invoker_sa_email" {
  description = "Service account to set as OIDC token SA on the Gmail Pub/Sub push subscription."
  value       = google_service_account.pubsub_invoker.email
}

output "feedback_invoker_sa_email" {
  description = "Service account to grant run.invoker for external feedback webhook callers."
  value       = google_service_account.feedback_invoker.email
}
