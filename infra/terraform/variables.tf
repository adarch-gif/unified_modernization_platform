variable "project_id" {
  description = "Google Cloud project ID that will host the platform."
  type        = string
}

variable "region" {
  description = "Primary GCP region for Cloud Run and Artifact Registry."
  type        = string
}

variable "name_prefix" {
  description = "Prefix used for Terraform-managed resource names."
  type        = string
  default     = "ump"
}

variable "environment" {
  description = "Runtime environment passed to the deployed services."
  type        = string
  default     = "prod"
}

variable "gateway_image" {
  description = "Container image for the Cloud Run gateway service."
  type        = string
  default     = ""
}

variable "harness_image" {
  description = "Optional container image for the Cloud Run harness job. Falls back to gateway_image."
  type        = string
  default     = ""
}

variable "deploy_gateway_service" {
  description = "Whether to deploy the HTTP gateway service to Cloud Run."
  type        = bool
  default     = true
}

variable "deploy_harness_job" {
  description = "Whether to deploy the gateway harness as a Cloud Run Job."
  type        = bool
  default     = true
}

variable "gateway_allow_unauthenticated" {
  description = "Whether to grant allUsers the Cloud Run invoker role. API-key middleware still applies inside the service."
  type        = bool
  default     = false
}

variable "gateway_min_instances" {
  description = "Minimum number of Cloud Run instances for the gateway service."
  type        = number
  default     = 0
}

variable "gateway_max_instances" {
  description = "Maximum number of Cloud Run instances for the gateway service."
  type        = number
  default     = 10
}

variable "gateway_container_concurrency" {
  description = "Maximum concurrent requests per Cloud Run gateway instance."
  type        = number
  default     = 80
}

variable "gateway_timeout_seconds" {
  description = "Request timeout for the Cloud Run gateway service."
  type        = number
  default     = 30
}

variable "gateway_cpu" {
  description = "CPU limit for the Cloud Run gateway service."
  type        = string
  default     = "1"
}

variable "gateway_memory" {
  description = "Memory limit for the Cloud Run gateway service."
  type        = string
  default     = "512Mi"
}

variable "gateway_max_body_bytes" {
  description = "Maximum request body size enforced by the HTTP gateway."
  type        = number
  default     = 65536
}

variable "harness_task_timeout_seconds" {
  description = "Timeout for a single Cloud Run Job execution of the harness."
  type        = number
  default     = 1800
}

variable "harness_cpu" {
  description = "CPU limit for the Cloud Run harness job."
  type        = string
  default     = "1"
}

variable "harness_memory" {
  description = "Memory limit for the Cloud Run harness job."
  type        = string
  default     = "512Mi"
}

variable "harness_cases_file" {
  description = "Path inside the container image to the JSON/JSONL case file used by the harness job."
  type        = string
  default     = "examples/search_harness_cases.jsonl"
}

variable "harness_concurrency" {
  description = "Concurrent in-flight requests for the harness job."
  type        = number
  default     = 8
}

variable "harness_iterations" {
  description = "How many times the harness replays the configured case set."
  type        = number
  default     = 20
}

variable "harness_warmup_iterations" {
  description = "Warmup iterations run before the measured harness loop."
  type        = number
  default     = 2
}

variable "gateway_mode" {
  description = "Traffic mode for SearchGatewayService."
  type        = string
  default     = "shadow"

  validation {
    condition     = contains(["azure_only", "shadow", "canary", "elastic_only"], lower(var.gateway_mode))
    error_message = "gateway_mode must be one of: azure_only, shadow, canary, elastic_only."
  }
}

variable "canary_percent" {
  description = "Percent of traffic routed to Elasticsearch in canary mode."
  type        = number
  default     = 0

  validation {
    condition     = var.canary_percent >= 0 && var.canary_percent <= 100
    error_message = "canary_percent must be between 0 and 100."
  }
}

variable "shadow_observation_percent" {
  description = "Percent of requests observed on the shadow path during canary or frozen-canary operation."
  type        = number
  default     = 25

  validation {
    condition     = var.shadow_observation_percent >= 0 && var.shadow_observation_percent <= 100
    error_message = "shadow_observation_percent must be between 0 and 100."
  }
}

variable "auto_disable_canary_on_regression" {
  description = "Whether judged relevance regressions automatically freeze the canary."
  type        = bool
  default     = true
}

variable "gateway_azure_timeout_seconds" {
  description = "Timeout applied by the resilient wrapper for Azure AI Search."
  type        = number
  default     = 2.0
}

variable "gateway_elastic_timeout_seconds" {
  description = "Timeout applied by the resilient wrapper for Elasticsearch."
  type        = number
  default     = 1.0
}

variable "gateway_max_retries" {
  description = "Retry count applied by the resilient wrapper around search backends."
  type        = number
  default     = 2
}

variable "gateway_failure_threshold" {
  description = "Consecutive failures that open the circuit breaker."
  type        = number
  default     = 5
}

variable "gateway_recovery_timeout_seconds" {
  description = "Recovery timeout before the circuit breaker probes in half-open state."
  type        = number
  default     = 30.0
}

variable "field_map" {
  description = "Logical-to-physical field mappings used by OData translation."
  type        = map(string)
  default     = {}
}

variable "dedicated_tenants" {
  description = "Tenants that should use dedicated Elasticsearch aliases and routing."
  type        = set(string)
  default     = []
}

variable "azure_search_endpoint" {
  description = "Azure AI Search endpoint used by the gateway and harness."
  type        = string
  default     = ""
}

variable "azure_search_default_index" {
  description = "Default Azure AI Search index."
  type        = string
  default     = ""
}

variable "azure_search_index_map" {
  description = "Optional entity-type to Azure AI Search index map."
  type        = map(string)
  default     = {}
}

variable "azure_search_api_version" {
  description = "Azure AI Search API version."
  type        = string
  default     = "2025-09-01"
}

variable "azure_search_document_id_field" {
  description = "Document ID field used when normalizing Azure AI Search responses."
  type        = string
  default     = "id"
}

variable "azure_search_auth_mode" {
  description = "How the gateway authenticates to Azure AI Search: api_key or bearer_token."
  type        = string
  default     = "api_key"

  validation {
    condition     = contains(["api_key", "bearer_token"], lower(var.azure_search_auth_mode))
    error_message = "azure_search_auth_mode must be api_key or bearer_token."
  }
}

variable "elasticsearch_endpoint" {
  description = "Elasticsearch endpoint used by the gateway and harness."
  type        = string
  default     = ""
}

variable "elasticsearch_default_index" {
  description = "Default Elasticsearch read alias or index."
  type        = string
  default     = ""
}

variable "elasticsearch_index_map" {
  description = "Optional entity-type to Elasticsearch read alias map."
  type        = map(string)
  default     = {}
}

variable "elasticsearch_document_id_field" {
  description = "Document ID field used when normalizing Elasticsearch responses."
  type        = string
  default     = "id"
}

variable "elasticsearch_auth_mode" {
  description = "How the gateway authenticates to Elasticsearch: api_key or bearer_token."
  type        = string
  default     = "api_key"

  validation {
    condition     = contains(["api_key", "bearer_token"], lower(var.elasticsearch_auth_mode))
    error_message = "elasticsearch_auth_mode must be api_key or bearer_token."
  }
}

variable "telemetry_mode" {
  description = "Telemetry sink mode for deployed workloads."
  type        = string
  default     = "logger"

  validation {
    condition     = contains(["noop", "memory", "logger", "otlp_http"], lower(var.telemetry_mode))
    error_message = "telemetry_mode must be one of: noop, memory, logger, otlp_http."
  }
}

variable "telemetry_service_name" {
  description = "Telemetry service name reported by the OpenTelemetry sink."
  type        = string
  default     = "unified-modernization-platform"
}

variable "otlp_collector_endpoint" {
  description = "OTLP HTTP collector endpoint when telemetry_mode is otlp_http."
  type        = string
  default     = ""
}

variable "publisher_endpoint" {
  description = "Elasticsearch endpoint for the projection publisher, documented for future worker deployment."
  type        = string
  default     = ""
}

variable "publisher_refresh" {
  description = "Refresh mode used by the projection publisher."
  type        = string
  default     = ""
}

variable "publisher_auth_mode" {
  description = "How the future publisher will authenticate to Elasticsearch: api_key or bearer_token."
  type        = string
  default     = "api_key"

  validation {
    condition     = contains(["api_key", "bearer_token"], lower(var.publisher_auth_mode))
    error_message = "publisher_auth_mode must be api_key or bearer_token."
  }
}

variable "publisher_write_alias_map" {
  description = "Optional entity-type to Elasticsearch write alias map for the publisher."
  type        = map(string)
  default     = {}
}

variable "spanner_config" {
  description = "Spanner instance configuration, for example regional-us-central1."
  type        = string
}

variable "spanner_processing_units" {
  description = "Processing units allocated to the projection-state Spanner instance."
  type        = number
  default     = 100
}

variable "spanner_database_name" {
  description = "Spanner database name for projection state."
  type        = string
  default     = "projection-state"
}

variable "firestore_database_name" {
  description = "Firestore database name. Use (default) unless you operate named databases."
  type        = string
  default     = "(default)"
}

variable "firestore_location" {
  description = "Firestore location, for example us-central or nam5."
  type        = string
}

variable "firestore_collection_prefix" {
  description = "Collection prefix for FirestoreCutoverStateStore."
  type        = string
  default     = "cutover_transitions"
}

variable "secret_values" {
  description = "Optional secret payloads to seed into Secret Manager. Keys: gateway_api_keys, azure_search_credential, elasticsearch_credential, publisher_credential, otlp_headers."
  type        = map(string)
  default     = {}
  sensitive   = true

  validation {
    condition = length(setsubtract(
      toset(keys(var.secret_values)),
      toset(["gateway_api_keys", "azure_search_credential", "elasticsearch_credential", "publisher_credential", "otlp_headers"])
    )) == 0
    error_message = "secret_values only supports: gateway_api_keys, azure_search_credential, elasticsearch_credential, publisher_credential, otlp_headers."
  }
}

variable "secret_version_refs" {
  description = "Optional Secret Manager version aliases or numbers for secrets managed outside Terraform. Keys: gateway_api_keys, azure_search_credential, elasticsearch_credential, publisher_credential, otlp_headers."
  type        = map(string)
  default     = {}

  validation {
    condition = length(setsubtract(
      toset(keys(var.secret_version_refs)),
      toset(["gateway_api_keys", "azure_search_credential", "elasticsearch_credential", "publisher_credential", "otlp_headers"])
    )) == 0
    error_message = "secret_version_refs only supports: gateway_api_keys, azure_search_credential, elasticsearch_credential, publisher_credential, otlp_headers."
  }
}
