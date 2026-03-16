from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any, Protocol

from unified_modernization.gateway.odata import ODataTranslator


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
        mode: TrafficMode = TrafficMode.AZURE_ONLY,
        canary_percent: int = 0,
    ) -> None:
        self._azure = azure_backend
        self._elastic = elastic_backend
        self._translator = translator or ODataTranslator()
        self._mode = mode
        self._canary_percent = canary_percent
        self.last_shadow_comparison: dict[str, Any] | None = None

    async def search(self, consumer_id: str, raw_params: dict[str, str]) -> dict[str, Any]:
        if self._mode == TrafficMode.AZURE_ONLY:
            return await self._azure.query({"params": raw_params})
        if self._mode == TrafficMode.ELASTIC_ONLY:
            return await self._elastic.query(self._translator.translate(raw_params))
        if self._mode == TrafficMode.SHADOW:
            primary = await self._azure.query({"params": raw_params})
            shadow = await self._elastic.query(self._translator.translate(raw_params))
            self.last_shadow_comparison = self._compare(primary, shadow)
            return primary
        if self._bucket(consumer_id) < self._canary_percent:
            return await self._elastic.query(self._translator.translate(raw_params))
        return await self._azure.query({"params": raw_params})

    def _bucket(self, consumer_id: str) -> int:
        return int(hashlib.md5(consumer_id.encode("utf-8")).hexdigest(), 16) % 100

    def _compare(self, primary: dict[str, Any], shadow: dict[str, Any]) -> dict[str, Any]:
        primary_ids = [item.get("id") for item in primary.get("results", [])]
        shadow_ids = [item.get("id") for item in shadow.get("results", [])]
        overlap = len(set(primary_ids).intersection(shadow_ids))
        return {
            "primary_count": len(primary_ids),
            "shadow_count": len(shadow_ids),
            "overlap": overlap,
            "identical_order": primary_ids == shadow_ids,
        }
