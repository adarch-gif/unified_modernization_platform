from __future__ import annotations

import hashlib
from enum import StrEnum
import threading
from typing import Any, Protocol

from unified_modernization.gateway.evaluation import (
    QueryJudgment,
    SearchEvaluationHarness,
    ShadowQualityDecision,
    ShadowQualityGate,
)
from unified_modernization.gateway.odata import ODataTranslator
from unified_modernization.observability.telemetry import NoopTelemetrySink, TelemetryEvent, TelemetrySink
from unified_modernization.routing.tenant_policy import TenantPolicyEngine


class SearchBackend(Protocol):
    async def query(self, request: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class QueryJudgmentProvider(Protocol):
    def get_judgment(
        self,
        *,
        tenant_id: str,
        entity_type: str,
        raw_params: dict[str, str],
        primary: dict[str, Any],
        shadow: dict[str, Any],
    ) -> QueryJudgment | None:
        raise NotImplementedError


class TrafficMode(StrEnum):
    AZURE_ONLY = "azure_only"
    SHADOW = "shadow"
    CANARY = "canary"
    ELASTIC_ONLY = "elastic_only"


class SearchGatewayService:
    def __init__(
        self,
        azure_backend: SearchBackend,
        elastic_backend: SearchBackend,
        translator: ODataTranslator | None = None,
        tenant_policy_engine: TenantPolicyEngine | None = None,
        evaluator: SearchEvaluationHarness | None = None,
        quality_gate: ShadowQualityGate | None = None,
        judgment_provider: QueryJudgmentProvider | None = None,
        telemetry_sink: TelemetrySink | None = None,
        mode: TrafficMode = TrafficMode.AZURE_ONLY,
        canary_percent: int = 0,
        auto_disable_canary_on_regression: bool = True,
        shadow_observation_percent: int = 100,
    ) -> None:
        self._azure = azure_backend
        self._elastic = elastic_backend
        self._translator = translator or ODataTranslator()
        self._tenant_policy_engine = tenant_policy_engine or TenantPolicyEngine()
        self._evaluator = evaluator or SearchEvaluationHarness()
        self._quality_gate = quality_gate or ShadowQualityGate()
        self._judgment_provider = judgment_provider
        self._telemetry_sink = telemetry_sink or NoopTelemetrySink()
        self._mode = mode
        self._canary_percent = canary_percent
        self._auto_disable_canary_on_regression = auto_disable_canary_on_regression
        self._shadow_observation_percent = shadow_observation_percent
        self._state_lock = threading.RLock()
        self._canary_frozen = False
        self._last_canary_freeze_event: dict[str, Any] | None = None
        self._last_shadow_comparison: dict[str, Any] | None = None
        self._last_shadow_quality_gate: dict[str, Any] | None = None

    @property
    def last_shadow_comparison(self) -> dict[str, Any] | None:
        with self._state_lock:
            return None if self._last_shadow_comparison is None else dict(self._last_shadow_comparison)

    @last_shadow_comparison.setter
    def last_shadow_comparison(self, value: dict[str, Any] | None) -> None:
        with self._state_lock:
            self._last_shadow_comparison = None if value is None else dict(value)

    @property
    def last_shadow_quality_gate(self) -> dict[str, Any] | None:
        with self._state_lock:
            return None if self._last_shadow_quality_gate is None else dict(self._last_shadow_quality_gate)

    @last_shadow_quality_gate.setter
    def last_shadow_quality_gate(self, value: dict[str, Any] | None) -> None:
        with self._state_lock:
            self._last_shadow_quality_gate = None if value is None else dict(value)

    @property
    def canary_frozen(self) -> bool:
        with self._state_lock:
            return self._canary_frozen

    @canary_frozen.setter
    def canary_frozen(self, value: bool) -> None:
        with self._state_lock:
            self._canary_frozen = value

    @property
    def last_canary_freeze_event(self) -> dict[str, Any] | None:
        with self._state_lock:
            return None if self._last_canary_freeze_event is None else dict(self._last_canary_freeze_event)

    @last_canary_freeze_event.setter
    def last_canary_freeze_event(self, value: dict[str, Any] | None) -> None:
        with self._state_lock:
            self._last_canary_freeze_event = None if value is None else dict(value)

    async def search(
        self,
        consumer_id: str,
        tenant_id: str,
        entity_type: str,
        raw_params: dict[str, str],
    ) -> dict[str, Any]:
        trace_id = raw_params.get("trace_id")
        azure_request = {
            "params": raw_params,
            "tenant_id": tenant_id,
            "entity_type": entity_type,
            "trace_id": trace_id,
        }
        self.last_shadow_comparison = None
        self.last_shadow_quality_gate = None
        with self._telemetry_sink.start_span(
            "search.gateway.request",
            trace_id=trace_id,
            attributes={
                "mode": self._mode.value,
                "tenant_id": tenant_id,
                "entity_type": entity_type,
            },
        ):
            self._telemetry_sink.increment(
                "search.gateway.requests",
                tags={"mode": self._mode.value, "entity_type": entity_type},
            )
            if self._mode == TrafficMode.AZURE_ONLY:
                return await self._azure.query(azure_request)
            if self._mode == TrafficMode.ELASTIC_ONLY:
                elastic_request = self._build_elastic_request(tenant_id, entity_type, raw_params)
                return await self._elastic.query(elastic_request)
            if self._mode == TrafficMode.SHADOW:
                primary = await self._azure.query(azure_request)
                try:
                    elastic_request = self._build_elastic_request(tenant_id, entity_type, raw_params)
                    shadow = await self._elastic.query(elastic_request)
                except Exception as exc:
                    self._record_shadow_failure(
                        error=exc,
                        mode=self._mode,
                        entity_type=entity_type,
                        trace_id=trace_id,
                        primary_backend="azure",
                        shadow_backend="elastic",
                    )
                    return primary
                self.last_shadow_comparison = self._compare(primary, shadow)
                self.last_shadow_quality_gate = self._evaluate_shadow_quality(
                    tenant_id=tenant_id,
                    entity_type=entity_type,
                    raw_params=raw_params,
                    primary=primary,
                    shadow=shadow,
                    live_comparison=self.last_shadow_comparison,
                )
                return primary
            if self._mode == TrafficMode.CANARY and self.canary_frozen:
                self._telemetry_sink.increment("search.gateway.canary_frozen", tags={"backend": "azure"})
                primary = await self._azure.query(azure_request)
                if not self._should_observe_shadow(tenant_id=tenant_id, consumer_id=consumer_id, entity_type=entity_type):
                    self._telemetry_sink.increment("search.gateway.shadow_sampled_out", tags={"mode": self._mode.value})
                    return primary
                try:
                    elastic_request = self._build_elastic_request(tenant_id, entity_type, raw_params)
                    shadow = await self._elastic.query(elastic_request)
                except Exception as exc:
                    self._record_shadow_failure(
                        error=exc,
                        mode=self._mode,
                        entity_type=entity_type,
                        trace_id=trace_id,
                        primary_backend="azure",
                        shadow_backend="elastic",
                    )
                    return primary
                self.last_shadow_comparison = self._compare(primary, shadow)
                self.last_shadow_quality_gate = self._evaluate_shadow_quality(
                    tenant_id=tenant_id,
                    entity_type=entity_type,
                    raw_params=raw_params,
                    primary=primary,
                    shadow=shadow,
                    live_comparison=self.last_shadow_comparison,
                )
                return primary
            if self._bucket(f"{tenant_id}:{consumer_id}") < self._canary_percent:
                self._telemetry_sink.increment("search.gateway.canary_routed", tags={"backend": "elastic"})
                elastic_request = self._build_elastic_request(tenant_id, entity_type, raw_params)
                canary_response = await self._elastic.query(elastic_request)
                if not self._should_observe_shadow(tenant_id=tenant_id, consumer_id=consumer_id, entity_type=entity_type):
                    self._telemetry_sink.increment("search.gateway.shadow_sampled_out", tags={"mode": self._mode.value})
                    return canary_response
                try:
                    azure_shadow = await self._azure.query(azure_request)
                except Exception as exc:
                    self._record_shadow_failure(
                        error=exc,
                        mode=self._mode,
                        entity_type=entity_type,
                        trace_id=trace_id,
                        primary_backend="elastic",
                        shadow_backend="azure",
                    )
                    return canary_response
                self.last_shadow_comparison = self._compare(azure_shadow, canary_response)
                self.last_shadow_quality_gate = self._evaluate_shadow_quality(
                    tenant_id=tenant_id,
                    entity_type=entity_type,
                    raw_params=raw_params,
                    primary=azure_shadow,
                    shadow=canary_response,
                    live_comparison=self.last_shadow_comparison,
                )
                return canary_response
            self._telemetry_sink.increment("search.gateway.canary_routed", tags={"backend": "azure"})
            primary = await self._azure.query(azure_request)
            if not self._should_observe_shadow(tenant_id=tenant_id, consumer_id=consumer_id, entity_type=entity_type):
                self._telemetry_sink.increment("search.gateway.shadow_sampled_out", tags={"mode": self._mode.value})
                return primary
            try:
                elastic_request = self._build_elastic_request(tenant_id, entity_type, raw_params)
                shadow = await self._elastic.query(elastic_request)
            except Exception as exc:
                self._record_shadow_failure(
                    error=exc,
                    mode=self._mode,
                    entity_type=entity_type,
                    trace_id=trace_id,
                    primary_backend="azure",
                    shadow_backend="elastic",
                )
                return primary
            self.last_shadow_comparison = self._compare(primary, shadow)
            self.last_shadow_quality_gate = self._evaluate_shadow_quality(
                tenant_id=tenant_id,
                entity_type=entity_type,
                raw_params=raw_params,
                primary=primary,
                shadow=shadow,
                live_comparison=self.last_shadow_comparison,
            )
            return primary
        raise RuntimeError(f"unsupported traffic mode {self._mode}")

    def _bucket(self, consumer_id: str) -> int:
        return int(hashlib.md5(consumer_id.encode("utf-8"), usedforsecurity=False).hexdigest(), 16) % 100

    def _should_observe_shadow(self, *, tenant_id: str, consumer_id: str, entity_type: str) -> bool:
        if self._shadow_observation_percent >= 100:
            return True
        if self._shadow_observation_percent <= 0:
            return False
        return self._bucket(f"shadow:{tenant_id}:{consumer_id}:{entity_type}") < self._shadow_observation_percent

    def _build_elastic_request(self, tenant_id: str, entity_type: str, raw_params: dict[str, str]) -> dict[str, Any]:
        policy = self._tenant_policy_engine.resolve(tenant_id, entity_type)
        return {
            "tenant_id": tenant_id,
            "entity_type": entity_type,
            "alias": policy.read_alias,
            "routing": policy.routing_key,
            "trace_id": raw_params.get("trace_id"),
            "query": self._translator.translate(raw_params),
        }

    def _compare(self, primary: dict[str, Any], shadow: dict[str, Any]) -> dict[str, Any]:
        return self._evaluator.compare_live(primary, shadow)

    def _evaluate_shadow_quality(
        self,
        *,
        tenant_id: str,
        entity_type: str,
        raw_params: dict[str, str],
        primary: dict[str, Any],
        shadow: dict[str, Any],
        live_comparison: dict[str, Any],
    ) -> dict[str, Any] | None:
        query_id = raw_params.get("query_id")
        judgment = None
        if self._judgment_provider is not None:
            judgment = self._judgment_provider.get_judgment(
                tenant_id=tenant_id,
                entity_type=entity_type,
                raw_params=raw_params,
                primary=primary,
                shadow=shadow,
            )

        primary_metrics = None
        shadow_metrics = None
        if judgment is not None:
            effective_query_id = query_id or judgment.query_id
            primary_metrics = self._evaluator.evaluate_response(
                query_id=effective_query_id,
                response=primary,
                judgment=judgment,
            )
            shadow_metrics = self._evaluator.evaluate_response(
                query_id=effective_query_id,
                response=shadow,
                judgment=judgment,
            )
            query_id = effective_query_id

        decision = self._quality_gate.evaluate(
            live_comparison=live_comparison,
            query_id=query_id,
            primary_metrics=primary_metrics,
            shadow_metrics=shadow_metrics,
        )
        payload = self._decision_payload(decision)
        if live_comparison.get("identical_order") is False:
            self._telemetry_sink.increment(
                "search.shadow.order_mismatch",
                tags={"entity_type": entity_type},
            )
        if decision.event is not None:
            self._telemetry_sink.increment(
                "search.shadow.regression",
                tags={"code": decision.event.code, "entity_type": entity_type},
            )
            self._telemetry_sink.emit(payload)
            self._maybe_disable_canary(decision, entity_type=entity_type)
        elif shadow_metrics is not None:
            self._telemetry_sink.emit(
                TelemetryEvent(
                    event_type="shadow_quality_healthy",
                    trace_id=query_id,
                    attributes={
                        "entity_type": entity_type,
                        "shadow_ndcg_at_10": shadow_metrics.ndcg_at_10,
                        "shadow_mrr": shadow_metrics.mrr,
                    },
                )
            )
        return payload

    def _maybe_disable_canary(
        self,
        decision: ShadowQualityDecision,
        *,
        entity_type: str,
    ) -> None:
        if not self._auto_disable_canary_on_regression or self._mode != TrafficMode.CANARY:
            return
        if decision.event is None:
            return
        self.canary_frozen = True
        self._canary_percent = 0
        event = {
            "event_type": "canary_auto_disabled",
            "severity": "high",
            "attributes": {
                "entity_type": entity_type,
                "code": decision.event.code,
                "query_id": decision.event.query_id or "",
            },
        }
        self.last_canary_freeze_event = event
        self._telemetry_sink.increment(
            "search.gateway.canary_auto_disabled",
            tags={"entity_type": entity_type, "code": decision.event.code},
        )
        self._telemetry_sink.emit(event)

    def _record_shadow_failure(
        self,
        *,
        error: Exception,
        mode: TrafficMode,
        entity_type: str,
        trace_id: str | None,
        primary_backend: str,
        shadow_backend: str,
    ) -> None:
        self.last_shadow_comparison = None
        self.last_shadow_quality_gate = None
        self._telemetry_sink.increment(
            "search.shadow.backend_failure",
            tags={"mode": mode.value, "entity_type": entity_type, "shadow_backend": shadow_backend},
        )
        self._telemetry_sink.emit(
            TelemetryEvent(
                event_type="search_shadow_backend_failure",
                severity="warning",
                trace_id=trace_id,
                attributes={
                    "mode": mode.value,
                    "entity_type": entity_type,
                    "primary_backend": primary_backend,
                    "shadow_backend": shadow_backend,
                    "error_type": type(error).__name__,
                },
            )
        )

    @staticmethod
    def _decision_payload(decision: ShadowQualityDecision) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "live_comparison": decision.live_comparison,
            "judged_metrics": decision.judged_metrics,
        }
        if decision.event is not None:
            payload["event"] = decision.event.model_dump()
        return payload
