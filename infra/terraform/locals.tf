locals {
  resource_prefix         = trimspace(var.name_prefix) != "" ? lower(replace(trimspace(var.name_prefix), "_", "-")) : "ump"
  service_account_prefix  = substr(local.resource_prefix, 0, 18)
  gateway_image           = trimspace(var.gateway_image)
  harness_image           = trimspace(var.harness_image) != "" ? trimspace(var.harness_image) : trimspace(var.gateway_image)
  dedicated_tenants_env   = join(",", sort(tolist(var.dedicated_tenants)))
  field_map_env           = join(",", [for key in sort(keys(var.field_map)) : "${key}=${var.field_map[key]}"])
  azure_index_map_env     = join(",", [for key in sort(keys(var.azure_search_index_map)) : "${key}=${var.azure_search_index_map[key]}"])
  elastic_index_map_env   = join(",", [for key in sort(keys(var.elasticsearch_index_map)) : "${key}=${var.elasticsearch_index_map[key]}"])
  publisher_alias_map_env = join(",", [for key in sort(keys(var.publisher_write_alias_map)) : "${key}=${var.publisher_write_alias_map[key]}"])
  supplied_secret_values  = { for key, value in var.secret_values : key => trimspace(value) if trimspace(value) != "" }
  external_secret_refs    = { for key, value in var.secret_version_refs : key => trimspace(value) if trimspace(value) != "" }
  non_production_envs     = toset(["local", "dev", "test"])

  secret_catalog = {
    gateway_api_keys         = "${local.resource_prefix}-gateway-api-keys"
    azure_search_credential  = "${local.resource_prefix}-azure-search-credential"
    elasticsearch_credential = "${local.resource_prefix}-elasticsearch-credential"
    publisher_credential     = "${local.resource_prefix}-publisher-credential"
    otlp_headers             = "${local.resource_prefix}-otlp-headers"
  }

  gateway_plain_env = [
    { name = "PORT", value = "8080" },
    { name = "GATEWAY_ENVIRONMENT", value = var.environment },
    { name = "GATEWAY_MAX_BODY_BYTES", value = tostring(var.gateway_max_body_bytes) },
    { name = "GATEWAY_FIELD_MAP", value = local.field_map_env },
    { name = "UMP_ENVIRONMENT", value = var.environment },
    { name = "UMP_GATEWAY_MODE", value = var.gateway_mode },
    { name = "UMP_GATEWAY_CANARY_PERCENT", value = tostring(var.canary_percent) },
    { name = "UMP_GATEWAY_AUTO_DISABLE_CANARY_ON_REGRESSION", value = tostring(var.auto_disable_canary_on_regression) },
    { name = "UMP_GATEWAY_SHADOW_OBSERVATION_PERCENT", value = tostring(var.shadow_observation_percent) },
    { name = "UMP_GATEWAY_AZURE_TIMEOUT_SECONDS", value = tostring(var.gateway_azure_timeout_seconds) },
    { name = "UMP_GATEWAY_ELASTIC_TIMEOUT_SECONDS", value = tostring(var.gateway_elastic_timeout_seconds) },
    { name = "UMP_GATEWAY_MAX_RETRIES", value = tostring(var.gateway_max_retries) },
    { name = "UMP_GATEWAY_FAILURE_THRESHOLD", value = tostring(var.gateway_failure_threshold) },
    { name = "UMP_GATEWAY_RECOVERY_TIMEOUT_SECONDS", value = tostring(var.gateway_recovery_timeout_seconds) },
    { name = "UMP_GATEWAY_FIELD_MAP", value = local.field_map_env },
    { name = "UMP_DEDICATED_TENANTS", value = local.dedicated_tenants_env },
    { name = "UMP_AZURE_SEARCH_ENDPOINT", value = var.azure_search_endpoint },
    { name = "UMP_AZURE_SEARCH_DEFAULT_INDEX", value = var.azure_search_default_index },
    { name = "UMP_AZURE_SEARCH_INDEX_MAP", value = local.azure_index_map_env },
    { name = "UMP_AZURE_SEARCH_API_VERSION", value = var.azure_search_api_version },
    { name = "UMP_AZURE_SEARCH_DOCUMENT_ID_FIELD", value = var.azure_search_document_id_field },
    { name = "UMP_ELASTICSEARCH_ENDPOINT", value = var.elasticsearch_endpoint },
    { name = "UMP_ELASTICSEARCH_DEFAULT_INDEX", value = var.elasticsearch_default_index },
    { name = "UMP_ELASTICSEARCH_INDEX_MAP", value = local.elastic_index_map_env },
    { name = "UMP_ELASTICSEARCH_DOCUMENT_ID_FIELD", value = var.elasticsearch_document_id_field },
    { name = "UMP_TELEMETRY_MODE", value = var.telemetry_mode },
    { name = "UMP_TELEMETRY_SERVICE_NAME", value = var.telemetry_service_name },
    { name = "UMP_OTLP_COLLECTOR_ENDPOINT", value = var.otlp_collector_endpoint },
  ]

  harness_plain_env = [
    { name = "UMP_ENVIRONMENT", value = var.environment },
    { name = "UMP_GATEWAY_MODE", value = var.gateway_mode },
    { name = "UMP_GATEWAY_CANARY_PERCENT", value = tostring(var.canary_percent) },
    { name = "UMP_GATEWAY_AUTO_DISABLE_CANARY_ON_REGRESSION", value = tostring(var.auto_disable_canary_on_regression) },
    { name = "UMP_GATEWAY_SHADOW_OBSERVATION_PERCENT", value = tostring(var.shadow_observation_percent) },
    { name = "UMP_GATEWAY_AZURE_TIMEOUT_SECONDS", value = tostring(var.gateway_azure_timeout_seconds) },
    { name = "UMP_GATEWAY_ELASTIC_TIMEOUT_SECONDS", value = tostring(var.gateway_elastic_timeout_seconds) },
    { name = "UMP_GATEWAY_MAX_RETRIES", value = tostring(var.gateway_max_retries) },
    { name = "UMP_GATEWAY_FAILURE_THRESHOLD", value = tostring(var.gateway_failure_threshold) },
    { name = "UMP_GATEWAY_RECOVERY_TIMEOUT_SECONDS", value = tostring(var.gateway_recovery_timeout_seconds) },
    { name = "UMP_GATEWAY_FIELD_MAP", value = local.field_map_env },
    { name = "UMP_DEDICATED_TENANTS", value = local.dedicated_tenants_env },
    { name = "UMP_AZURE_SEARCH_ENDPOINT", value = var.azure_search_endpoint },
    { name = "UMP_AZURE_SEARCH_DEFAULT_INDEX", value = var.azure_search_default_index },
    { name = "UMP_AZURE_SEARCH_INDEX_MAP", value = local.azure_index_map_env },
    { name = "UMP_AZURE_SEARCH_API_VERSION", value = var.azure_search_api_version },
    { name = "UMP_AZURE_SEARCH_DOCUMENT_ID_FIELD", value = var.azure_search_document_id_field },
    { name = "UMP_ELASTICSEARCH_ENDPOINT", value = var.elasticsearch_endpoint },
    { name = "UMP_ELASTICSEARCH_DEFAULT_INDEX", value = var.elasticsearch_default_index },
    { name = "UMP_ELASTICSEARCH_INDEX_MAP", value = local.elastic_index_map_env },
    { name = "UMP_ELASTICSEARCH_DOCUMENT_ID_FIELD", value = var.elasticsearch_document_id_field },
    { name = "UMP_TELEMETRY_MODE", value = var.telemetry_mode },
    { name = "UMP_TELEMETRY_SERVICE_NAME", value = var.telemetry_service_name },
    { name = "UMP_OTLP_COLLECTOR_ENDPOINT", value = var.otlp_collector_endpoint },
  ]

  gateway_secret_env = concat(
    contains(local.non_production_envs, lower(var.environment)) ? [] : [
      { name = "GATEWAY_API_KEYS", secret_key = "gateway_api_keys" }
    ],
    [
      {
        name       = var.azure_search_auth_mode == "api_key" ? "UMP_AZURE_SEARCH_API_KEY" : "UMP_AZURE_SEARCH_BEARER_TOKEN"
        secret_key = "azure_search_credential"
      },
      {
        name       = var.elasticsearch_auth_mode == "api_key" ? "UMP_ELASTICSEARCH_API_KEY" : "UMP_ELASTICSEARCH_BEARER_TOKEN"
        secret_key = "elasticsearch_credential"
      },
    ],
    var.telemetry_mode == "otlp_http" && contains(keys(merge(local.supplied_secret_values, local.external_secret_refs)), "otlp_headers") ? [
      { name = "UMP_OTLP_HEADERS", secret_key = "otlp_headers" }
    ] : []
  )

  harness_secret_env = concat(
    [
      {
        name       = var.azure_search_auth_mode == "api_key" ? "UMP_AZURE_SEARCH_API_KEY" : "UMP_AZURE_SEARCH_BEARER_TOKEN"
        secret_key = "azure_search_credential"
      },
      {
        name       = var.elasticsearch_auth_mode == "api_key" ? "UMP_ELASTICSEARCH_API_KEY" : "UMP_ELASTICSEARCH_BEARER_TOKEN"
        secret_key = "elasticsearch_credential"
      },
    ],
    var.telemetry_mode == "otlp_http" && contains(keys(merge(local.supplied_secret_values, local.external_secret_refs)), "otlp_headers") ? [
      { name = "UMP_OTLP_HEADERS", secret_key = "otlp_headers" }
    ] : []
  )

  gateway_secret_access_keys = toset([for env in local.gateway_secret_env : env.secret_key])
  harness_secret_access_keys = toset([for env in local.harness_secret_env : env.secret_key])
  projection_runtime_secret_access_keys = toset(compact([
    trimspace(var.publisher_endpoint) != "" ? "publisher_credential" : null,
    var.telemetry_mode == "otlp_http" && contains(keys(merge(local.supplied_secret_values, local.external_secret_refs)), "otlp_headers") ? "otlp_headers" : null,
  ]))

  projection_publisher_env = {
    UMP_ENVIRONMENT               = var.environment
    UMP_PUBLISHER_ENDPOINT        = var.publisher_endpoint
    UMP_PUBLISHER_REFRESH         = var.publisher_refresh
    UMP_DEDICATED_TENANTS         = local.dedicated_tenants_env
    UMP_PUBLISHER_WRITE_ALIAS_MAP = local.publisher_alias_map_env
  }

  projection_runtime_substrate_hints = {
    FIRESTORE_COLLECTION_PREFIX   = var.firestore_collection_prefix
    SPANNER_INSTANCE              = substr("${local.resource_prefix}-projection", 0, 30)
    SPANNER_DATABASE              = var.spanner_database_name
  }
}
