# ── Firestore (Native mode) ───────────────────────────────────────────────────
# Firestore Native is the recommended mode for new projects.
# Collections are created implicitly by the application on first write.
#
# T10: the classifier SA is granted datastore.user at the project level (main.tf)
# which is the narrowest IAM primitive available for Firestore in Terraform
# (collection-level IAM requires the Firestore REST API / firebase-tools, not TF).
# In the application layer (src/feedback/store.py) access is further scoped to
# the "classifications" collection only — the SA has no admin / rules-edit permissions.

resource "google_firestore_database" "main" {
  project     = var.gcp_project_id
  name        = "${local.name_prefix}-db"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  depends_on = [google_project_service.apis]
}

# Composite index for querying classifications by intent + time
resource "google_firestore_index" "intent_time" {
  project    = var.gcp_project_id
  database   = google_firestore_database.main.name
  collection = "classifications"

  fields {
    field_path = "intent"
    order      = "ASCENDING"
  }
  fields {
    field_path = "classified_at"
    order      = "DESCENDING"
  }
}
