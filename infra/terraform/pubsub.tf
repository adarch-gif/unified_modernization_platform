resource "google_pubsub_topic" "projection_events" {
  project = var.project_id
  name    = "${local.resource_prefix}-projection-events"

  depends_on = [google_project_service.required]
}

resource "google_pubsub_topic" "projection_dead_letter" {
  project = var.project_id
  name    = "${local.resource_prefix}-projection-events-dlq"

  depends_on = [google_project_service.required]
}

resource "google_pubsub_topic" "projection_publish_dead_letter" {
  project = var.project_id
  name    = "${local.resource_prefix}-projection-publish-dlq"

  depends_on = [google_project_service.required]
}

resource "google_pubsub_topic" "cutover_transitions" {
  project = var.project_id
  name    = "${local.resource_prefix}-cutover-transitions"

  depends_on = [google_project_service.required]
}

resource "google_pubsub_topic" "reconciliation_repair" {
  project = var.project_id
  name    = "${local.resource_prefix}-reconciliation-repair"

  depends_on = [google_project_service.required]
}

resource "google_pubsub_subscription" "projection_worker" {
  project = var.project_id
  name    = "${local.resource_prefix}-projection-worker"
  topic   = google_pubsub_topic.projection_events.name

  ack_deadline_seconds       = 30
  message_retention_duration = "604800s"

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.projection_dead_letter.id
    max_delivery_attempts = 10
  }
}
