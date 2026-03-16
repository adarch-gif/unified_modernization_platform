output "artifact_registry_repository" {
  description = "Artifact Registry repository for the platform images."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.containers.repository_id}"
}

output "gateway_service_url" {
  description = "Cloud Run URL for the HTTP search gateway."
  value       = var.deploy_gateway_service ? google_cloud_run_v2_service.gateway[0].uri : null
}

output "gateway_service_name" {
  description = "Cloud Run service name for the HTTP gateway."
  value       = var.deploy_gateway_service ? google_cloud_run_v2_service.gateway[0].name : null
}

output "harness_job_name" {
  description = "Cloud Run Job name for the gateway harness."
  value       = var.deploy_harness_job ? google_cloud_run_v2_job.gateway_harness[0].name : null
}

output "service_accounts" {
  description = "Service accounts provisioned for gateway, harness, and future projection runtime workloads."
  value = {
    gateway            = google_service_account.gateway.email
    harness            = google_service_account.harness.email
    projection_runtime = google_service_account.projection_runtime.email
  }
}

output "secret_names" {
  description = "Secret Manager secret IDs provisioned by Terraform."
  value       = { for key, secret in google_secret_manager_secret.managed : key => secret.secret_id }
}

output "projection_state_spanner" {
  description = "Spanner instance and database for projection state."
  value = {
    instance = google_spanner_instance.projection_state.name
    database = google_spanner_database.projection_state.name
  }
}

output "firestore_cutover" {
  description = "Firestore database used for durable cutover state."
  value = {
    database_name      = google_firestore_database.cutover.name
    collection_prefix  = var.firestore_collection_prefix
    firestore_location = var.firestore_location
  }
}

output "pubsub_topics" {
  description = "Provisioned Pub/Sub topics that back the projection and reconciliation substrate."
  value = {
    projection_events              = google_pubsub_topic.projection_events.name
    projection_dead_letter         = google_pubsub_topic.projection_dead_letter.name
    projection_publish_dead_letter = google_pubsub_topic.projection_publish_dead_letter.name
    cutover_transitions            = google_pubsub_topic.cutover_transitions.name
    reconciliation_repair          = google_pubsub_topic.reconciliation_repair.name
  }
}

output "projection_publisher_environment" {
  description = "Environment variables already consumed by the projection publisher bootstrap."
  value       = local.projection_publisher_env
}

output "projection_runtime_substrate_hints" {
  description = "Substrate hints for a future projection worker. These are not env vars consumed by current code."
  value       = local.projection_runtime_substrate_hints
}
