from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any, Protocol

from unified_modernization.gateway.evaluation import SearchEvaluationHarness
from unified_modernization.gateway.odata import ODataTranslator
from unified_modernization.routing.tenant_policy import TenantPolicyEngine


class SearchBackend(Protocol):
    async def query(self, request: dict[str, Any]) -> dict[str, Any]:
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
        mode: TrafficMode = TrafficMode.AZURE_ONLY,
        canary_percent: int = 0,
    ) -> None:
        self._azure = azure_backend
        self._elastic = elastic_backend
        self._translator = translator or ODataTranslator()
        self._tenant_policy_engine = tenant_policy_engine or TenantPolicyEngine()
        self._evaluator = evaluator or SearchEvaluationHarness()
        self._mode = mode
        self._canary_percent = canary_percent
        self.last_shadow_comparison: dict[str, Any] | None = None

    async def search(
        self,
        consumer_id: str,
        tenant_id: str,
        entity_type: str,
        raw_params: dict[str, str],
    ) -> dict[str, Any]:
        azure_request = {
            "params": raw_params,
            "tenant_id": tenant_id,
            "entity_type": entity_type,
        }
        elastic_request = self._build_elastic_request(tenant_id, entity_type, raw_params)
        if self._mode == TrafficMode.AZURE_ONLY:
            return await self._azure.query(azure_request)
        if self._mode == TrafficMode.ELASTIC_ONLY:
            return await self._elastic.query(elastic_request)
        if self._mode == TrafficMode.SHADOW:
            primary = await self._azure.query(azure_request)
            shadow = await self._elastic.query(elastic_request)
            self.last_shadow_comparison = self._compare(primary, shadow)
            return primary
        if self._bucket(f"{tenant_id}:{consumer_id}") < self._canary_percent:
            return await self._elastic.query(elastic_request)
        return await self._azure.query(azure_request)

    def _bucket(self, consumer_id: str) -> int:
        return int(hashlib.md5(consumer_id.encode("utf-8")).hexdigest(), 16) % 100

    def _build_elastic_request(self, tenant_id: str, entity_type: str, raw_params: dict[str, str]) -> dict[str, Any]:
        policy = self._tenant_policy_engine.resolve(tenant_id, entity_type)
        return {
            "tenant_id": tenant_id,
            "entity_type": entity_type,
            "alias": policy.read_alias,
            "routing": policy.routing_key,
            "query": self._translator.translate(raw_params),
        }

    def _compare(self, primary: dict[str, Any], shadow: dict[str, Any]) -> dict[str, Any]:
        return self._evaluator.compare_live(primary, shadow)
