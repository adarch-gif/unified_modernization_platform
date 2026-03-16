from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, Field

from unified_modernization.gateway.clients import (
    AzureAISearchBackend,
    AzureSearchBackendConfig,
    ElasticsearchBackendConfig,
    ElasticsearchSearchBackend,
)
from unified_modernization.gateway.evaluation import SearchEvaluationHarness, ShadowQualityGate
from unified_modernization.gateway.odata import ODataTranslator
from unified_modernization.gateway.resilience import ResilientSearchBackend
from unified_modernization.gateway.service import QueryJudgmentProvider, SearchBackend, SearchGatewayService, TrafficMode
from unified_modernization.observability.telemetry import NoopTelemetrySink, TelemetrySink
from unified_modernization.routing.tenant_policy import TenantPolicyEngine


_NON_PRODUCTION_ENVIRONMENTS = {"local", "dev", "test"}


class GatewayRuntimeConfig(BaseModel):
    environment: str = "dev"
    mode: TrafficMode = TrafficMode.AZURE_ONLY
    canary_percent: int = Field(default=0, ge=0, le=100)
    auto_disable_canary_on_regression: bool = True
    shadow_observation_percent: int = Field(default=25, ge=0, le=100)
    azure_timeout_seconds: float = Field(default=2.0, gt=0)
    elastic_timeout_seconds: float = Field(default=1.0, gt=0)
    max_retries: int = Field(default=2, ge=0)
    failure_threshold: int = Field(default=5, ge=1)
    recovery_timeout_seconds: float = Field(default=30.0, gt=0)


class AzureGatewayBackendConfig(BaseModel):
    endpoint: str
    default_index_name: str | None = None
    index_names_by_entity_type: dict[str, str] = Field(default_factory=dict)
    api_version: str = "2025-09-01"
    api_key: str | None = None
    bearer_token: str | None = None
    document_id_field: str = "id"

    def to_backend_config(self) -> AzureSearchBackendConfig:
        return AzureSearchBackendConfig(**self.model_dump())


class ElasticsearchGatewayBackendConfig(BaseModel):
    endpoint: str
    default_index_name: str | None = None
    index_names_by_entity_type: dict[str, str] = Field(default_factory=dict)
    api_key: str | None = None
    bearer_token: str | None = None
    document_id_field: str = "id"

    def to_backend_config(self) -> ElasticsearchBackendConfig:
        return ElasticsearchBackendConfig(**self.model_dump())


class GatewayIntegrationConfig(BaseModel):
    runtime: GatewayRuntimeConfig = Field(default_factory=GatewayRuntimeConfig)
    azure: AzureGatewayBackendConfig
    elastic: ElasticsearchGatewayBackendConfig
    field_map: dict[str, str] = Field(default_factory=dict)
    dedicated_tenants: set[str] = Field(default_factory=set)


def build_search_gateway_service(
    *,
    azure_backend: SearchBackend,
    elastic_backend: SearchBackend,
    config: GatewayRuntimeConfig,
    translator: ODataTranslator | None = None,
    tenant_policy_engine: TenantPolicyEngine | None = None,
    evaluator: SearchEvaluationHarness | None = None,
    quality_gate: ShadowQualityGate | None = None,
    judgment_provider: QueryJudgmentProvider | None = None,
    telemetry_sink: TelemetrySink | None = None,
) -> SearchGatewayService:
    telemetry = telemetry_sink or NoopTelemetrySink()
    environment = config.environment.lower()
    if environment not in _NON_PRODUCTION_ENVIRONMENTS and isinstance(telemetry, NoopTelemetrySink):
        raise ValueError("telemetry sink is required outside local/dev/test environments")

    resolved_azure_backend = (
        azure_backend
        if isinstance(azure_backend, ResilientSearchBackend)
        else ResilientSearchBackend(
            azure_backend,
            name="azure",
            timeout_seconds=config.azure_timeout_seconds,
            max_retries=config.max_retries,
            failure_threshold=config.failure_threshold,
            recovery_timeout_seconds=config.recovery_timeout_seconds,
            telemetry_sink=telemetry,
        )
    )
    resolved_elastic_backend = (
        elastic_backend
        if isinstance(elastic_backend, ResilientSearchBackend)
        else ResilientSearchBackend(
            elastic_backend,
            name="elastic",
            timeout_seconds=config.elastic_timeout_seconds,
            max_retries=config.max_retries,
            failure_threshold=config.failure_threshold,
            recovery_timeout_seconds=config.recovery_timeout_seconds,
            telemetry_sink=telemetry,
        )
    )

    return SearchGatewayService(
        azure_backend=resolved_azure_backend,
        elastic_backend=resolved_elastic_backend,
        translator=translator,
        tenant_policy_engine=tenant_policy_engine,
        evaluator=evaluator,
        quality_gate=quality_gate,
        judgment_provider=judgment_provider,
        telemetry_sink=telemetry,
        mode=config.mode,
        canary_percent=config.canary_percent,
        auto_disable_canary_on_regression=config.auto_disable_canary_on_regression,
        shadow_observation_percent=config.shadow_observation_percent,
    )


def build_http_search_gateway_service(
    *,
    config: GatewayIntegrationConfig,
    judgment_provider: QueryJudgmentProvider | None = None,
    telemetry_sink: TelemetrySink | None = None,
    evaluator: SearchEvaluationHarness | None = None,
    quality_gate: ShadowQualityGate | None = None,
    azure_backend: SearchBackend | None = None,
    elastic_backend: SearchBackend | None = None,
) -> SearchGatewayService:
    translator = ODataTranslator(config.field_map)
    tenant_policy_engine = TenantPolicyEngine(dedicated_tenants=set(config.dedicated_tenants))
    return build_search_gateway_service(
        azure_backend=azure_backend or AzureAISearchBackend(config.azure.to_backend_config()),
        elastic_backend=elastic_backend or ElasticsearchSearchBackend(config.elastic.to_backend_config()),
        config=config.runtime,
        translator=translator,
        tenant_policy_engine=tenant_policy_engine,
        evaluator=evaluator,
        quality_gate=quality_gate,
        judgment_provider=judgment_provider,
        telemetry_sink=telemetry_sink,
    )


def load_gateway_integration_config_from_env(
    env: Mapping[str, str] | None = None,
    *,
    prefix: str = "UMP_",
) -> GatewayIntegrationConfig:
    source = env or os.environ
    return GatewayIntegrationConfig(
        runtime=GatewayRuntimeConfig(
            environment=source.get(f"{prefix}ENVIRONMENT", "dev").strip().lower(),
            mode=_parse_mode(
                source.get(f"{prefix}GATEWAY_MODE", TrafficMode.AZURE_ONLY.value),
                field_name=f"{prefix}GATEWAY_MODE",
            ),
            canary_percent=_parse_int(
                source.get(f"{prefix}GATEWAY_CANARY_PERCENT", "0"),
                field_name=f"{prefix}GATEWAY_CANARY_PERCENT",
            ),
            auto_disable_canary_on_regression=_parse_bool(
                source.get(f"{prefix}GATEWAY_AUTO_DISABLE_CANARY_ON_REGRESSION", "true")
            ),
            shadow_observation_percent=_parse_int(
                source.get(f"{prefix}GATEWAY_SHADOW_OBSERVATION_PERCENT", "25"),
                field_name=f"{prefix}GATEWAY_SHADOW_OBSERVATION_PERCENT",
            ),
            azure_timeout_seconds=_parse_float(
                source.get(f"{prefix}GATEWAY_AZURE_TIMEOUT_SECONDS", "2.0"),
                field_name=f"{prefix}GATEWAY_AZURE_TIMEOUT_SECONDS",
            ),
            elastic_timeout_seconds=_parse_float(
                source.get(f"{prefix}GATEWAY_ELASTIC_TIMEOUT_SECONDS", "1.0"),
                field_name=f"{prefix}GATEWAY_ELASTIC_TIMEOUT_SECONDS",
            ),
            max_retries=_parse_int(
                source.get(f"{prefix}GATEWAY_MAX_RETRIES", "2"),
                field_name=f"{prefix}GATEWAY_MAX_RETRIES",
            ),
            failure_threshold=_parse_int(
                source.get(f"{prefix}GATEWAY_FAILURE_THRESHOLD", "5"),
                field_name=f"{prefix}GATEWAY_FAILURE_THRESHOLD",
            ),
            recovery_timeout_seconds=_parse_float(
                source.get(f"{prefix}GATEWAY_RECOVERY_TIMEOUT_SECONDS", "30.0"),
                field_name=f"{prefix}GATEWAY_RECOVERY_TIMEOUT_SECONDS",
            ),
        ),
        azure=AzureGatewayBackendConfig(
            endpoint=_required_str(source.get(f"{prefix}AZURE_SEARCH_ENDPOINT"), field_name=f"{prefix}AZURE_SEARCH_ENDPOINT"),
            default_index_name=_optional_str(source.get(f"{prefix}AZURE_SEARCH_DEFAULT_INDEX")),
            index_names_by_entity_type=_parse_mapping(source.get(f"{prefix}AZURE_SEARCH_INDEX_MAP")),
            api_version=source.get(f"{prefix}AZURE_SEARCH_API_VERSION", "2025-09-01").strip(),
            api_key=_optional_str(source.get(f"{prefix}AZURE_SEARCH_API_KEY")),
            bearer_token=_optional_str(source.get(f"{prefix}AZURE_SEARCH_BEARER_TOKEN")),
            document_id_field=source.get(f"{prefix}AZURE_SEARCH_DOCUMENT_ID_FIELD", "id").strip(),
        ),
        elastic=ElasticsearchGatewayBackendConfig(
            endpoint=_required_str(source.get(f"{prefix}ELASTICSEARCH_ENDPOINT"), field_name=f"{prefix}ELASTICSEARCH_ENDPOINT"),
            default_index_name=_optional_str(source.get(f"{prefix}ELASTICSEARCH_DEFAULT_INDEX")),
            index_names_by_entity_type=_parse_mapping(source.get(f"{prefix}ELASTICSEARCH_INDEX_MAP")),
            api_key=_optional_str(source.get(f"{prefix}ELASTICSEARCH_API_KEY")),
            bearer_token=_optional_str(source.get(f"{prefix}ELASTICSEARCH_BEARER_TOKEN")),
            document_id_field=source.get(f"{prefix}ELASTICSEARCH_DOCUMENT_ID_FIELD", "id").strip(),
        ),
        field_map=_parse_mapping(source.get(f"{prefix}GATEWAY_FIELD_MAP")),
        dedicated_tenants=_parse_set(source.get(f"{prefix}DEDICATED_TENANTS")),
    )


def build_http_search_gateway_service_from_env(
    *,
    judgment_provider: QueryJudgmentProvider | None = None,
    telemetry_sink: TelemetrySink | None = None,
    evaluator: SearchEvaluationHarness | None = None,
    quality_gate: ShadowQualityGate | None = None,
    env: Mapping[str, str] | None = None,
    prefix: str = "UMP_",
) -> SearchGatewayService:
    return build_http_search_gateway_service(
        config=load_gateway_integration_config_from_env(env, prefix=prefix),
        judgment_provider=judgment_provider,
        telemetry_sink=telemetry_sink,
        evaluator=evaluator,
        quality_gate=quality_gate,
    )


def _parse_mapping(raw: str | None) -> dict[str, str]:
    if raw is None or not raw.strip():
        return {}
    pairs: dict[str, str] = {}
    for item in raw.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            continue
        pairs[key] = value.strip()
    return pairs


def _parse_set(raw: str | None) -> set[str]:
    if raw is None or not raw.strip():
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def _parse_bool(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in {"true", "1", "yes", "on"}


def _optional_str(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _required_str(value: str | None, *, field_name: str) -> str:
    stripped = _optional_str(value)
    if stripped is None:
        raise ValueError(f"{field_name} is required")
    return stripped


def _parse_int(value: str | None, *, field_name: str) -> int:
    try:
        return int(_required_str(value, field_name=field_name))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an integer") from exc


def _parse_float(value: str | None, *, field_name: str) -> float:
    try:
        return float(_required_str(value, field_name=field_name))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a float") from exc


def _parse_mode(value: str | None, *, field_name: str) -> TrafficMode:
    normalized = _required_str(value, field_name=field_name).strip().lower()
    try:
        return TrafficMode(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be one of: {', '.join(mode.value for mode in TrafficMode)}") from exc
