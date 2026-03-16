resource "google_cloud_run_v2_service" "gateway" {
  count    = var.deploy_gateway_service ? 1 : 0
  name     = "${local.resource_prefix}-gateway"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account                  = google_service_account.gateway.email
    timeout                          = "${var.gateway_timeout_seconds}s"
    max_instance_request_concurrency = var.gateway_container_concurrency

    scaling {
      min_instance_count = var.gateway_min_instances
      max_instance_count = var.gateway_max_instances
    }

    containers {
      image   = local.gateway_image
      command = ["python", "-m", "uvicorn", "unified_modernization.gateway.http_api:app", "--host", "0.0.0.0", "--port", "8080"]

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.gateway_cpu
          memory = var.gateway_memory
        }
      }

      dynamic "env" {
        for_each = local.gateway_plain_env
        content {
          name  = env.value.name
          value = env.value.value
        }
      }

      dynamic "env" {
        for_each = local.gateway_secret_env
        content {
          name = env.value.name
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.managed[env.value.secret_key].secret_id
              version = contains(keys(data.google_secret_manager_secret_version.external), env.value.secret_key) ? data.google_secret_manager_secret_version.external[env.value.secret_key].version : google_secret_manager_secret_version.managed[env.value.secret_key].version
            }
          }
        }
      }
    }
  }

  depends_on = [
    terraform_data.validate_inputs,
    google_project_service.required,
    google_secret_manager_secret_iam_member.gateway_access,
    google_secret_manager_secret_version.managed,
    data.google_secret_manager_secret_version.external,
  ]
}

resource "google_cloud_run_v2_service_iam_member" "gateway_public_invoker" {
  count    = var.deploy_gateway_service && var.gateway_allow_unauthenticated ? 1 : 0
  location = var.region
  name     = google_cloud_run_v2_service.gateway[0].name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_job" "gateway_harness" {
  count    = var.deploy_harness_job ? 1 : 0
  name     = "${local.resource_prefix}-gateway-harness"
  location = var.region

  template {
    template {
      service_account = google_service_account.harness.email
      timeout         = "${var.harness_task_timeout_seconds}s"
      max_retries     = 1

      containers {
        image   = local.harness_image
        command = ["python", "-m", "unified_modernization.gateway.harness"]
        args = [
          "--cases-file",
          var.harness_cases_file,
          "--concurrency",
          tostring(var.harness_concurrency),
          "--iterations",
          tostring(var.harness_iterations),
          "--warmup-iterations",
          tostring(var.harness_warmup_iterations),
        ]

        resources {
          limits = {
            cpu    = var.harness_cpu
            memory = var.harness_memory
          }
        }

        dynamic "env" {
          for_each = local.harness_plain_env
          content {
            name  = env.value.name
            value = env.value.value
          }
        }

        dynamic "env" {
          for_each = local.harness_secret_env
          content {
            name = env.value.name
            value_source {
              secret_key_ref {
                secret  = google_secret_manager_secret.managed[env.value.secret_key].secret_id
                version = contains(keys(data.google_secret_manager_secret_version.external), env.value.secret_key) ? data.google_secret_manager_secret_version.external[env.value.secret_key].version : google_secret_manager_secret_version.managed[env.value.secret_key].version
              }
            }
          }
        }
      }
    }
  }

  depends_on = [
    terraform_data.validate_inputs,
    google_project_service.required,
    google_secret_manager_secret_iam_member.harness_access,
    google_secret_manager_secret_version.managed,
    data.google_secret_manager_secret_version.external,
  ]
}
