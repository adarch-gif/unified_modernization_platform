from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field


class AzureSearchBackendConfig(BaseModel):
    endpoint: str
    default_index_name: str | None = None
    index_names_by_entity_type: dict[str, str] = Field(default_factory=dict)
    api_version: str = "2025-09-01"
    api_key: str | None = None
    bearer_token: str | None = None
    document_id_field: str = "id"

    def resolve_index_name(self, entity_type: str) -> str:
        index_name = self.index_names_by_entity_type.get(entity_type, self.default_index_name)
        if not index_name:
            raise ValueError(f"no Azure AI Search index configured for entity_type={entity_type!r}")
        return index_name


class ElasticsearchBackendConfig(BaseModel):
    endpoint: str
    default_index_name: str | None = None
    index_names_by_entity_type: dict[str, str] = Field(default_factory=dict)
    api_key: str | None = None
    bearer_token: str | None = None
    document_id_field: str = "id"

    def resolve_index_name(self, entity_type: str) -> str:
        index_name = self.index_names_by_entity_type.get(entity_type, self.default_index_name)
        if not index_name:
            raise ValueError(f"no Elasticsearch index configured for entity_type={entity_type!r}")
        return index_name


class AzureAISearchBackend:
    def __init__(
        self,
        config: AzureSearchBackendConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient()

    async def query(self, request: dict[str, Any]) -> dict[str, Any]:
        entity_type = _require_str(request.get("entity_type"), field_name="entity_type")
        raw_params = _normalize_string_mapping(request.get("params"), field_name="params")
        trace_id = _optional_str(request.get("trace_id"))
        index_name = self._config.resolve_index_name(entity_type)
        response = await self._client.post(
            self._build_url(f"/indexes/{quote(index_name, safe='')}/docs/search"),
            params={"api-version": self._config.api_version},
            headers=self._headers(trace_id=trace_id),
            json=self._build_search_body(raw_params),
        )
        response.raise_for_status()
        return self._normalize_response(response.json())

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self, *, trace_id: str | None) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["api-key"] = self._config.api_key
        elif self._config.bearer_token:
            headers["Authorization"] = f"Bearer {self._config.bearer_token}"
        if trace_id:
            headers["x-ms-client-request-id"] = trace_id
        return headers

    def _build_search_body(self, raw_params: Mapping[str, str]) -> dict[str, Any]:
        body: dict[str, Any] = {"search": raw_params.get("$search", "*")}
        if filt := raw_params.get("$filter"):
            body["filter"] = filt
        if orderby := raw_params.get("$orderby"):
            body["orderby"] = orderby
        if select := raw_params.get("$select"):
            body["select"] = select
        if facets := raw_params.get("$facets") or raw_params.get("facet"):
            body["facets"] = [item.strip() for item in facets.split(",") if item.strip()]
        if top := raw_params.get("$top"):
            body["top"] = min(int(top), 1000)
        if skip := raw_params.get("$skip"):
            body["skip"] = int(skip)
        if _parse_bool(raw_params.get("$count")):
            body["count"] = True
        return body

    def _normalize_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_results = payload.get("value", [])
        results: list[dict[str, Any]] = []
        if isinstance(raw_results, list):
            for item in raw_results:
                if not isinstance(item, Mapping):
                    continue
                normalized = dict(item)
                document_id = normalized.get(self._config.document_id_field)
                if document_id is None:
                    document_id = normalized.get("id")
                if document_id is not None:
                    normalized["id"] = str(document_id)
                results.append(normalized)
        return {
            "results": results,
            "count": payload.get("@odata.count"),
            "facets": payload.get("@search.facets"),
            "raw": payload,
        }

    def _build_url(self, path: str) -> str:
        return f"{self._config.endpoint.rstrip('/')}{path}"


class ElasticsearchSearchBackend:
    def __init__(
        self,
        config: ElasticsearchBackendConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient()

    async def query(self, request: dict[str, Any]) -> dict[str, Any]:
        index_name = self._resolve_index_name(request)
        trace_id = _optional_str(request.get("trace_id"))
        routing = _optional_str(request.get("routing"))
        body = _normalize_query_body(request.get("query"))
        body.setdefault("track_total_hits", True)
        response = await self._client.post(
            self._build_url(f"/{quote(index_name, safe='')}/_search"),
            params={"routing": routing} if routing else None,
            headers=self._headers(trace_id=trace_id),
            json=body,
        )
        response.raise_for_status()
        return self._normalize_response(response.json())

    async def aclose(self) -> None:
        await self._client.aclose()

    def _resolve_index_name(self, request: dict[str, Any]) -> str:
        alias = _optional_str(request.get("alias"))
        if alias:
            return alias
        entity_type = _require_str(request.get("entity_type"), field_name="entity_type")
        return self._config.resolve_index_name(entity_type)

    def _headers(self, *, trace_id: str | None) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"ApiKey {self._config.api_key}"
        elif self._config.bearer_token:
            headers["Authorization"] = f"Bearer {self._config.bearer_token}"
        if trace_id:
            headers["X-Opaque-Id"] = trace_id
        return headers

    def _normalize_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        hits_section = payload.get("hits", {})
        raw_hits = hits_section.get("hits", []) if isinstance(hits_section, Mapping) else []
        results: list[dict[str, Any]] = []
        if isinstance(raw_hits, list):
            for item in raw_hits:
                if not isinstance(item, Mapping):
                    continue
                source = item.get("_source", {})
                normalized = dict(source) if isinstance(source, Mapping) else {}
                document_id = normalized.get(self._config.document_id_field, item.get("_id"))
                if document_id is not None:
                    normalized["id"] = str(document_id)
                if "_score" in item:
                    normalized["_score"] = item["_score"]
                results.append(normalized)

        total = None
        if isinstance(hits_section, Mapping):
            raw_total = hits_section.get("total")
            if isinstance(raw_total, Mapping):
                total_value = raw_total.get("value")
                total = int(total_value) if isinstance(total_value, int) else total_value
            elif isinstance(raw_total, int):
                total = raw_total

        return {
            "results": results,
            "count": total,
            "facets": payload.get("aggregations"),
            "raw": payload,
        }

    def _build_url(self, path: str) -> str:
        return f"{self._config.endpoint.rstrip('/')}{path}"


def _normalize_string_mapping(value: object, *, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping of strings")
    normalized: dict[str, str] = {}
    for key, item in value.items():
        normalized[str(key)] = str(item)
    return normalized


def _normalize_query_body(value: object) -> dict[str, Any]:
    if value is None:
        return {"query": {"match_all": {}}}
    if not isinstance(value, Mapping):
        raise ValueError("query must be a mapping")
    return dict(value)


def _require_str(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return str(value)
    return value


def _parse_bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"true", "1", "yes"}
