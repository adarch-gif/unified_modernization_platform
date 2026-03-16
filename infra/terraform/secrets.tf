resource "google_secret_manager_secret" "managed" {
  for_each = local.secret_catalog

  project   = var.project_id
  secret_id = each.value

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret_version" "managed" {
  for_each = local.supplied_secret_values

  secret      = google_secret_manager_secret.managed[each.key].id
  secret_data = each.value
}

data "google_secret_manager_secret_version" "external" {
  for_each = local.external_secret_refs

  secret  = google_secret_manager_secret.managed[each.key].id
  version = each.value

  depends_on = [google_secret_manager_secret.managed]
}

resource "google_secret_manager_secret_iam_member" "gateway_access" {
  for_each = { for key in local.gateway_secret_access_keys : key => google_secret_manager_secret.managed[key] }

  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.gateway.email}"
}

resource "google_secret_manager_secret_iam_member" "harness_access" {
  for_each = { for key in local.harness_secret_access_keys : key => google_secret_manager_secret.managed[key] }

  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.harness.email}"
}

resource "google_secret_manager_secret_iam_member" "projection_access" {
  for_each = { for key in local.projection_runtime_secret_access_keys : key => google_secret_manager_secret.managed[key] }

  project   = var.project_id
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.projection_runtime.email}"
}
