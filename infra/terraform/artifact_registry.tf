resource "google_artifact_registry_repository" "containers" {
  project       = var.project_id
  location      = var.region
  repository_id = substr("${local.resource_prefix}-containers", 0, 63)
  description   = "Container registry for the unified modernization platform"
  format        = "DOCKER"

  depends_on = [google_project_service.required]
}
