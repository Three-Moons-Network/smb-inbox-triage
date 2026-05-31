locals {
  name_prefix  = "${var.project_name}-${var.env}"
  common_labels = merge(
    {
      project = replace(var.project_name, "-", "_")
      env     = var.env
      cloud   = "gcp"
      owner   = "platform-team"
    },
    var.labels,
  )

  # Single image used by both Cloud Run services.
  # FUNCTION_TARGET env var selects handle_webhook vs handle_feedback at runtime.
  app_image = "${var.region}-docker.pkg.dev/${var.gcp_project_id}/${google_artifact_registry_repository.functions.repository_id}/smb-inbox-triage:${var.image_tag}"
}

provider "google" {
  project = var.gcp_project_id
  region  = var.region
}

# ── Enable required APIs ──────────────────────────────────────────────────────

resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "eventarc.googleapis.com",
    "pubsub.googleapis.com",
    "firestore.googleapis.com",
    "secretmanager.googleapis.com",
    "aiplatform.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# ── Service Account for Cloud Run services ────────────────────────────────────

resource "google_service_account" "classifier" {
  account_id   = "${local.name_prefix}-clf"
  display_name = "Inbox Triage Classifier SA (${var.env})"
}

# IAM bindings — principle of least privilege
resource "google_project_iam_member" "vertex_user" {
  project = var.gcp_project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.classifier.email}"
}

# T10/N3: Custom Firestore role scoped to minimum permissions.
#
# We cannot scope Terraform IAM to a single Firestore collection — the GCP
# Terraform provider only supports project-level Firestore IAM bindings.
# Mitigations applied:
#   1. Custom role grants only the three operations the classifier actually uses
#      (get, create, update) — not the full datastore.user role which also
#      includes list, query, and delete.
#   2. Firestore Security Rules (deployed separately via Firebase CLI) enforce
#      collection-level access — see firestore.rules in the repo root.
#   3. Accepted residual risk: the SA can operate on other Firestore collections
#      in the same project IF they exist and have no Security Rules. Mitigate by
#      ensuring no other collections exist in this project, or by moving to a
#      dedicated GCP project per service.
#
# ACCEPTED RISK: T10 — custom role restricts operations but not collection scope.
# Owner: gcp-dev | Review date: 2026-11

resource "google_project_iam_custom_role" "firestore_classifier" {
  role_id     = "firestoreClassifier"
  title       = "Firestore Classifier — inbox-triage read/write"
  description = "Minimum Firestore permissions for the SMB inbox-triage classifier SA."
  project     = var.gcp_project_id

  permissions = [
    "datastore.entities.get",
    "datastore.entities.create",
    "datastore.entities.update",
    # Required by Firestore client library to list databases at connection time
    "datastore.databases.get",
  ]
}

resource "google_project_iam_member" "datastore_user" {
  project = var.gcp_project_id
  role    = google_project_iam_custom_role.firestore_classifier.id
  member  = "serviceAccount:${google_service_account.classifier.email}"
}

resource "google_project_iam_member" "pubsub_publisher" {
  project = var.gcp_project_id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.classifier.email}"
}

resource "google_secret_manager_secret_iam_member" "slack_access" {
  secret_id = google_secret_manager_secret.slack_webhook.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.classifier.email}"
}

# ── Secret Manager — Datadog API key ─────────────────────────────────────────
# Create the secret shell here; populate the value with:
#   gcloud secrets versions add smb-inbox-triage-dd-api-key \
#     --data-file=<(echo -n "YOUR_DD_API_KEY") --project=<PROJECT_ID>
# or via the one-liner in GCP-DEPLOY.md.

resource "google_secret_manager_secret" "dd_api_key" {
  secret_id = var.dd_api_key_secret_name
  labels    = local.common_labels

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_iam_member" "dd_api_key_access" {
  secret_id = google_secret_manager_secret.dd_api_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.classifier.email}"
}

# ── Secret Manager — Slack webhook URL ───────────────────────────────────────

resource "google_secret_manager_secret" "slack_webhook" {
  secret_id = var.slack_webhook_secret_name
  labels    = local.common_labels

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

# ── Artifact Registry — container images ─────────────────────────────────────

resource "google_artifact_registry_repository" "functions" {
  location      = var.region
  repository_id = "${local.name_prefix}-functions"
  format        = "DOCKER"
  labels        = local.common_labels

  depends_on = [google_project_service.apis]
}

# Allow the Cloud Run runtime service account to pull container images from
# this Artifact Registry repo. Without this binding, Cloud Run create/update
# fails with "Image ... not found" (GCP returns NOT_FOUND rather than
# PERMISSION_DENIED to avoid leaking existence info).
resource "google_artifact_registry_repository_iam_member" "classifier_image_pull" {
  project    = var.gcp_project_id
  location   = google_artifact_registry_repository.functions.location
  repository = google_artifact_registry_repository.functions.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.classifier.email}"
}

# ── Cloud Run V2 — Classifier ─────────────────────────────────────────────────
#
# Observability architecture (mirrors AWS Lambda Extension pattern):
#
#   Python app (classifier container)
#       │  OTLP/HTTP  http://localhost:4318
#       ▼
#   Datadog Agent sidecar (shared network namespace within the Cloud Run instance)
#       │  HTTPS/443  → Datadog intake
#       ▼
#   Datadog APM + LLM Observability + Logs
#
# The app container does NOT hold DD_API_KEY — the sidecar handles Datadog auth.
# OTEL_EXPORTER_OTLP_ENDPOINT is set to http://localhost:4318 explicitly.

resource "google_cloud_run_v2_service" "classifier" {
  name     = "${local.name_prefix}-classifier"
  location = var.region
  labels   = local.common_labels
  ingress  = "INGRESS_TRAFFIC_ALL"

  # Provider default is `true`, which blocks `terraform destroy` with
  # "cannot destroy service without setting deletion_protection=false and
  # running terraform apply". This is a side-project dev env that we tear
  # down and rebuild — keep it false so destroy is single-pass.
  deletion_protection = false

  template {
    service_account = google_service_account.classifier.email
    timeout         = "${var.function_timeout_seconds}s"

    # CPU always-allocated during the instance lifetime — required so the
    # Datadog Agent sidecar can drain its queue to Datadog between requests
    # without being throttled to ~0 immediately post-response. Combined with
    # min_instance_count = 0 this still scales to zero (no idle cost) but
    # ensures the sidecar can finish its outbound HTTPS while the instance
    # is alive. This is the cost-optimal reliability fix; flip to
    # min_instance_count = 1 only if traces still drop on cold starts.
    annotations = {
      "run.googleapis.com/cpu-throttling" = "false"
    }

    scaling {
      max_instance_count = 10
      min_instance_count = 0
    }

    # ── App container ─────────────────────────────────────────────────────────
    containers {
      name  = "classifier"
      image = local.app_image

      resources {
        limits = {
          memory = "${var.function_memory_mb}Mi"
          cpu    = "1000m"
        }
      }

      ports {
        name           = "http1"
        container_port = 8080
      }

      env {
        name  = "FUNCTION_TARGET"
        value = "handle_webhook"
      }
      env {
        name  = "CLOUD"
        value = "gcp"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.gcp_project_id
      }
      env {
        name  = "VERTEX_AI_LOCATION"
        value = var.region
      }
      env {
        name  = "VERTEX_MODEL_ID"
        value = var.llm_model_id
      }
      env {
        name  = "PUBSUB_PROJECT_ID"
        value = var.gcp_project_id
      }
      env {
        name  = "SLACK_WEBHOOK_SECRET_NAME"
        value = var.slack_webhook_secret_name
      }
      env {
        name  = "FIRESTORE_DATABASE"
        value = google_firestore_database.main.name
      }
      env {
        name  = "DD_SERVICE"
        value = "smb-inbox-triage"
      }
      env {
        name  = "DD_ENV"
        value = var.env
      }
      env {
        name  = "DD_SITE"
        value = var.dd_site
      }
      env {
        name  = "DD_VERSION"
        value = "1.0.0"
      }
      env {
        name  = "OBSERVABILITY_ENABLED"
        value = "true"
      }
      # Route OTLP to the DD Agent sidecar — same network namespace, no auth needed
      env {
        name  = "OTEL_EXPORTER_OTLP_ENDPOINT"
        value = "http://localhost:4318"
      }
      # Datadog OTLP intake requires delta temporality; cumulative is dropped.
      env {
        name  = "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"
        value = "delta"
      }
      # Lower BSP flush cadence because Cloud Run V2 CPU is throttled between
      # requests — the BSP background thread can't run, so we lean on force_flush()
      # in the handler plus a shorter schedule so any batch built during a request
      # is more likely to be drained when CPU is available.
      env {
        name  = "OTEL_BSP_SCHEDULE_DELAY"
        value = "500"
      }
      # DD_API_KEY intentionally absent from this container — sidecar handles it.

      env {
        name = "SLACK_WEBHOOK_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.slack_webhook.secret_id
            version = "latest"
          }
        }
      }
    }

    # ── Datadog Agent sidecar ─────────────────────────────────────────────────
    # Receives OTLP/HTTP on 0.0.0.0:4318 (reachable at localhost:4318 from the
    # app container), then forwards traces to Datadog intake over HTTPS/443.
    containers {
      name  = "datadog-agent"
      image = "gcr.io/datadoghq/agent:7"

      resources {
        limits = {
          memory = "512Mi"
          cpu    = "500m"
        }
      }

      # OTLP receiver — listen on all interfaces within the instance
      env {
        name  = "DD_OTLP_CONFIG_RECEIVER_PROTOCOLS_HTTP_ENDPOINT"
        value = "0.0.0.0:4318"
      }
      # Bridge OTLP signals into the agent's downstream pipelines. Without these
      # explicit toggles, the agent accepts OTLP traces but does not bridge OTLP
      # metrics or logs to Datadog — so SDK-emitted metrics never appear.
      env {
        name  = "DD_APM_OTLP_ENABLED"
        value = "true"
      }
      env {
        name  = "DD_OTLP_CONFIG_TRACES_ENABLED"
        value = "true"
      }
      env {
        name  = "DD_OTLP_CONFIG_METRICS_ENABLED"
        value = "true"
      }
      env {
        name  = "DD_OTLP_CONFIG_LOGS_ENABLED"
        value = "true"
      }
      env {
        name  = "DD_SITE"
        value = var.dd_site
      }
      env {
        name  = "DD_SERVICE"
        value = "smb-inbox-triage"
      }
      env {
        name  = "DD_ENV"
        value = var.env
      }
      env {
        name  = "DD_VERSION"
        value = "1.0.0"
      }
      # Cloud Run V2 instances have no stable host identity; every auto-detect
      # path the agent tries (kubelet, EC2 metadata, /etc/hostname, gRPC peer)
      # fails and the CORE agent exits with "unable to reliably determine the
      # host name". Setting DD_HOSTNAME explicitly skips detection entirely.
      env {
        name  = "DD_HOSTNAME"
        value = "${local.name_prefix}-classifier"
      }
      env {
        name  = "DD_APM_ENABLED"
        value = "true"
      }
      # Enable logs pipeline so OTel log records sent via OTLP land in Datadog Logs.
      env {
        name  = "DD_LOGS_ENABLED"
        value = "true"
      }
      env {
        name  = "DD_PROCESS_AGENT_ENABLED"
        value = "false"
      }
      # SYS-PROBE has no role in Cloud Run and emits noisy vDSO ELF errors.
      env {
        name  = "DD_SYSTEM_PROBE_ENABLED"
        value = "false"
      }
      env {
        name  = "DD_DOGSTATSD_NON_LOCAL_TRAFFIC"
        value = "false"
      }
      env {
        name = "DD_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.dd_api_key.secret_id
            version = "latest"
          }
        }
      }

      # Wait for the OTLP receiver to be ready before the app sends spans
      startup_probe {
        tcp_socket      { port = 4318 }
        initial_delay_seconds = 5
        period_seconds        = 5
        failure_threshold     = 12
        timeout_seconds       = 3
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_iam_member.slack_access,
    google_secret_manager_secret_iam_member.dd_api_key_access,
  ]
}

# ── Cloud Run V2 — Feedback ───────────────────────────────────────────────────

resource "google_cloud_run_v2_service" "feedback" {
  name     = "${local.name_prefix}-feedback"
  location = var.region
  labels   = local.common_labels
  ingress  = "INGRESS_TRAFFIC_ALL"

  # Same reason as classifier above — destroy must be single-pass.
  deletion_protection = false

  template {
    service_account = google_service_account.classifier.email
    timeout         = "15s"

    # Same rationale as classifier service: CPU always-allocated lets the DD
    # Agent sidecar drain between requests; scale-to-zero keeps idle cost at $0.
    annotations = {
      "run.googleapis.com/cpu-throttling" = "false"
    }

    scaling {
      max_instance_count = 5
      min_instance_count = 0
    }

    # ── App container ─────────────────────────────────────────────────────────
    containers {
      name  = "feedback"
      image = local.app_image

      resources {
        limits = {
          # OTel SDK + protobuf imports use ~280Mi; 256Mi OOMs
          memory = "512Mi"
          cpu    = "1000m"
        }
      }

      ports {
        name           = "http1"
        container_port = 8080
      }

      env {
        name  = "FUNCTION_TARGET"
        value = "handle_feedback"
      }
      env {
        name  = "CLOUD"
        value = "gcp"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.gcp_project_id
      }
      env {
        name  = "FIRESTORE_DATABASE"
        value = google_firestore_database.main.name
      }
      env {
        name  = "DD_SERVICE"
        value = "smb-inbox-triage"
      }
      env {
        name  = "DD_ENV"
        value = var.env
      }
      env {
        name  = "DD_SITE"
        value = var.dd_site
      }
      env {
        name  = "DD_VERSION"
        value = "1.0.0"
      }
      env {
        name  = "OBSERVABILITY_ENABLED"
        value = "true"
      }
      env {
        name  = "OTEL_EXPORTER_OTLP_ENDPOINT"
        value = "http://localhost:4318"
      }
      env {
        name  = "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"
        value = "delta"
      }
      env {
        name  = "OTEL_BSP_SCHEDULE_DELAY"
        value = "500"
      }
    }

    # ── Datadog Agent sidecar ─────────────────────────────────────────────────
    containers {
      name  = "datadog-agent"
      image = "gcr.io/datadoghq/agent:7"

      resources {
        limits = {
          memory = "512Mi"
          cpu    = "500m"
        }
      }

      env {
        name  = "DD_OTLP_CONFIG_RECEIVER_PROTOCOLS_HTTP_ENDPOINT"
        value = "0.0.0.0:4318"
      }
      env {
        name  = "DD_SITE"
        value = var.dd_site
      }
      env {
        name  = "DD_SERVICE"
        value = "smb-inbox-triage"
      }
      env {
        name  = "DD_ENV"
        value = var.env
      }
      env {
        name  = "DD_VERSION"
        value = "1.0.0"
      }
      # See classifier sidecar above for rationale.
      env {
        name  = "DD_HOSTNAME"
        value = "${local.name_prefix}-feedback"
      }
      env {
        name  = "DD_APM_ENABLED"
        value = "true"
      }
      env {
        name  = "DD_LOGS_ENABLED"
        value = "false"
      }
      env {
        name  = "DD_PROCESS_AGENT_ENABLED"
        value = "false"
      }
      env {
        name  = "DD_SYSTEM_PROBE_ENABLED"
        value = "false"
      }
      env {
        name  = "DD_DOGSTATSD_NON_LOCAL_TRAFFIC"
        value = "false"
      }
      env {
        name = "DD_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.dd_api_key.secret_id
            version = "latest"
          }
        }
      }

      startup_probe {
        tcp_socket      { port = 4318 }
        initial_delay_seconds = 5
        period_seconds        = 5
        failure_threshold     = 12
        timeout_seconds       = 3
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_iam_member.dd_api_key_access,
  ]
}

# T1/T2: No unauthenticated access. Cloud Run services are invoked only by
# authenticated service accounts (OIDC JWT):
#   - classifier: pubsub_invoker SA (Eventarc/Pub/Sub push)
#   - feedback:   feedback_invoker SA (external webhook callers)

resource "google_service_account" "pubsub_invoker" {
  account_id   = "${local.name_prefix}-ps-inv"
  display_name = "Pub/Sub push invoker for inbox triage (${var.env})"
}

# Grant the Pub/Sub SA permission to invoke only the classifier service
resource "google_cloud_run_v2_service_iam_member" "classifier_invoker" {
  project  = var.gcp_project_id
  location = google_cloud_run_v2_service.classifier.location
  name     = google_cloud_run_v2_service.classifier.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.pubsub_invoker.email}"
}

# Grant the feedback SA permission to invoke only the feedback service
resource "google_service_account" "feedback_invoker" {
  account_id   = "${local.name_prefix}-fb-inv"
  display_name = "Feedback webhook invoker for inbox triage (${var.env})"
}

resource "google_cloud_run_v2_service_iam_member" "feedback_invoker" {
  project  = var.gcp_project_id
  location = google_cloud_run_v2_service.feedback.location
  name     = google_cloud_run_v2_service.feedback.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.feedback_invoker.email}"
}

# Allow Cloud Pub/Sub to create OIDC tokens for the invoker SA
resource "google_service_account_iam_member" "pubsub_token_creator" {
  service_account_id = google_service_account.pubsub_invoker.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

data "google_project" "current" {
  project_id = var.gcp_project_id
}

# ── Developer access — Secret Manager read (dev/debugging only) ───────────────
# Grants each email in var.developer_emails the ability to read secret values.
# Set developer_emails = [] in prod. This is the minimum permission needed to
# run `gcloud secrets versions access` or view secret values in the console.

resource "google_secret_manager_secret_iam_member" "developer_dd_key_access" {
  for_each  = toset(var.developer_emails)
  secret_id = google_secret_manager_secret.dd_api_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "user:${each.value}"
}

resource "google_secret_manager_secret_iam_member" "developer_slack_access" {
  for_each  = toset(var.developer_emails)
  secret_id = google_secret_manager_secret.slack_webhook.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "user:${each.value}"
}
