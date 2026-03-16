from __future__ import annotations

from pydantic import BaseModel, Field

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
    azure_timeout_seconds: float = Field(default=2.0, gt=0)
    elastic_timeout_seconds: float = Field(default=1.0, gt=0)
    max_retries: int = Field(default=2, ge=0)
    failure_threshold: int = Field(default=5, ge=1)
    recovery_timeout_seconds: float = Field(default=30.0, gt=0)


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
    )
