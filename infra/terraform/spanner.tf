resource "google_spanner_instance" "projection_state" {
  project          = var.project_id
  name             = substr("${local.resource_prefix}-projection", 0, 30)
  config           = var.spanner_config
  display_name     = "Unified Modernization Projection State"
  processing_units = var.spanner_processing_units

  depends_on = [google_project_service.required]
}

resource "google_spanner_database" "projection_state" {
  project                   = var.project_id
  instance                  = google_spanner_instance.projection_state.name
  name                      = var.spanner_database_name
  version_retention_period  = "1h"
  deletion_protection       = true
  ddl = [
    file("${path.module}/sql/projection_states.sql"),
    file("${path.module}/sql/projection_fragments.sql"),
  ]
}
