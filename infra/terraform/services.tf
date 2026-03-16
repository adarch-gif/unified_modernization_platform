resource "google_project_service" "required" {
  for_each = toset([
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "compute.googleapis.com",
    "firestore.googleapis.com",
    "iam.googleapis.com",
    "pubsub.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "spanner.googleapis.com",
  ])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "terraform_data" "validate_inputs" {
  input = {
    project_id = var.project_id
    region     = var.region
  }

  lifecycle {
    precondition {
      condition = length(setintersection(
        toset(keys(local.supplied_secret_values)),
        toset(keys(local.external_secret_refs))
      )) == 0
      error_message = "Use either secret_values or secret_version_refs for a given secret key, not both."
    }

    precondition {
      condition = !var.deploy_gateway_service || (
        local.gateway_image != "" &&
        trimspace(var.azure_search_endpoint) != "" &&
        trimspace(var.azure_search_default_index) != "" &&
        trimspace(var.elasticsearch_endpoint) != "" &&
        trimspace(var.elasticsearch_default_index) != "" &&
        contains(keys(merge(local.supplied_secret_values, local.external_secret_refs)), "azure_search_credential") &&
        contains(keys(merge(local.supplied_secret_values, local.external_secret_refs)), "elasticsearch_credential") &&
        (contains(local.non_production_envs, lower(var.environment)) || contains(keys(merge(local.supplied_secret_values, local.external_secret_refs)), "gateway_api_keys"))
      )
      error_message = "Gateway deployment requires gateway_image, Azure and Elasticsearch endpoints/indexes, backend credentials, and gateway_api_keys outside non-production environments, provided either through secret_values or secret_version_refs."
    }

    precondition {
      condition = !var.deploy_harness_job || (
        local.harness_image != "" &&
        trimspace(var.azure_search_endpoint) != "" &&
        trimspace(var.azure_search_default_index) != "" &&
        trimspace(var.elasticsearch_endpoint) != "" &&
        trimspace(var.elasticsearch_default_index) != "" &&
        contains(keys(merge(local.supplied_secret_values, local.external_secret_refs)), "azure_search_credential") &&
        contains(keys(merge(local.supplied_secret_values, local.external_secret_refs)), "elasticsearch_credential")
      )
      error_message = "Harness deployment requires a container image plus Azure and Elasticsearch endpoints, indexes, and credentials, provided either through secret_values or secret_version_refs."
    }

    precondition {
      condition = lower(var.telemetry_mode) != "otlp_http" || trimspace(var.otlp_collector_endpoint) != ""
      error_message = "otlp_collector_endpoint must be set when telemetry_mode is otlp_http."
    }
  }
}
