# ── Pub/Sub topics — one per intent class ─────────────────────────────────────

locals {
  intent_topics = [
    "sales-inquiry",
    "support-request",
    "billing-question",
    "vendor-outreach",
    "job-application",
    "urgent-escalation",
    "human-review",
    "marketing-noise",
  ]
}

resource "google_pubsub_topic" "intent_topics" {
  for_each = toset(local.intent_topics)
  name     = "${local.name_prefix}-${each.value}"
  labels   = local.common_labels

  message_retention_duration = "86400s"  # 24 hours
}

# ── Pub/Sub subscription for dead-letter ─────────────────────────────────────

resource "google_pubsub_topic" "dead_letter" {
  name   = "${local.name_prefix}-dead-letter"
  labels = local.common_labels
}

resource "google_pubsub_subscription" "dead_letter" {
  name  = "${local.name_prefix}-dead-letter-sub"
  topic = google_pubsub_topic.dead_letter.name
  labels = local.common_labels

  message_retention_duration = "1209600s"  # 14 days
  ack_deadline_seconds       = 600
}

# ── Gmail inbound Pub/Sub topic ───────────────────────────────────────────────
# Gmail Watch API pushes email change notifications to this topic.
# The Cloud Function is subscribed via an Eventarc trigger below.

resource "google_pubsub_topic" "gmail_inbound" {
  name   = "${local.name_prefix}-gmail-inbound"
  labels = local.common_labels
}

# Allow Gmail service account to publish to this topic
resource "google_pubsub_topic_iam_member" "gmail_publisher" {
  topic  = google_pubsub_topic.gmail_inbound.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:gmail-api-push@system.gserviceaccount.com"
}

# ── Eventarc trigger — Gmail Pub/Sub → Classifier function ───────────────────

resource "google_eventarc_trigger" "gmail_to_classifier" {
  name     = "${local.name_prefix}-gmail-trigger"
  location = var.region
  labels   = local.common_labels

  matching_criteria {
    attribute = "type"
    value     = "google.cloud.pubsub.topic.v1.messagePublished"
  }

  # For Pub/Sub event types, the source topic is specified here — NOT as a
  # matching_criteria filter.  The resourceName attribute is not supported
  # by the google.cloud.pubsub.topic.v1.messagePublished event type.
  transport {
    pubsub {
      topic = google_pubsub_topic.gmail_inbound.id
    }
  }

  destination {
    cloud_run_service {
      service = google_cloud_run_v2_service.classifier.name
      region  = var.region
      path    = "/webhook"
    }
  }

  # T1/T2: use the pubsub_invoker SA (not classifier SA and not allUsers)
  # This SA has run.invoker on the classifier Cloud Run service only.
  service_account = google_service_account.pubsub_invoker.email
}
