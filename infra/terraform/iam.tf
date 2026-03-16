resource "google_service_account" "gateway" {
  project      = var.project_id
  account_id   = "${local.service_account_prefix}-gw"
  display_name = "Unified Modernization Gateway"

  depends_on = [google_project_service.required]
}

resource "google_service_account" "harness" {
  project      = var.project_id
  account_id   = "${local.service_account_prefix}-hrns"
  display_name = "Unified Modernization Gateway Harness"

  depends_on = [google_project_service.required]
}

resource "google_service_account" "projection_runtime" {
  project      = var.project_id
  account_id   = "${local.service_account_prefix}-proj"
  display_name = "Unified Modernization Projection Runtime"

  depends_on = [google_project_service.required]
}

resource "google_project_iam_member" "projection_runtime_roles" {
  for_each = toset([
    "roles/datastore.user",
    "roles/pubsub.publisher",
    "roles/pubsub.subscriber",
    "roles/spanner.databaseUser",
  ])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.projection_runtime.email}"
}
