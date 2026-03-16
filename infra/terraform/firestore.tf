resource "google_firestore_database" "cutover" {
  provider                    = google-beta
  project                     = var.project_id
  name                        = var.firestore_database_name
  location_id                 = var.firestore_location
  type                        = "FIRESTORE_NATIVE"
  delete_protection_state     = "DELETE_PROTECTION_ENABLED"
  concurrency_mode            = "OPTIMISTIC"
  app_engine_integration_mode = "DISABLED"

  depends_on = [google_project_service.required]
}
