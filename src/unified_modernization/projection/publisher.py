from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

from unified_modernization.contracts.projection import CompletenessStatus, SearchDocument
from unified_modernization.observability.telemetry import NoopTelemetrySink, TelemetryEvent, TelemetrySink
from unified_modernization.routing.tenant_policy import TenantPolicyEngine


class SearchDocumentPublisher(Protocol):
    def publish(self, document: SearchDocument, *, trace_id: str | None = None) -> dict[str, Any]:
        raise NotImplementedError


class AsyncSearchDocumentPublisher(Protocol):
    async def publish_async(self, document: SearchDocument, *, trace_id: str | None = None) -> dict[str, Any]:
        raise NotImplementedError


class ElasticsearchPublisherConfig(BaseModel):
    endpoint: str
    api_key: str | None = None
    bearer_token: str | None = None
    refresh: str | None = None
    write_aliases_by_entity_type: dict[str, str] = Field(default_factory=dict)

    def resolve_write_alias(self, document: SearchDocument, tenant_policy_engine: TenantPolicyEngine) -> tuple[str, str | None]:
        alias = self.write_aliases_by_entity_type.get(document.entity_type)
        if alias is not None:
            return alias, document.tenant_id
        policy = tenant_policy_engine.resolve(document.tenant_id, document.entity_type)
        return policy.write_alias, policy.routing_key


class ElasticsearchDocumentPublisher:
    def __init__(
        self,
        config: ElasticsearchPublisherConfig,
        *,
        client: httpx.Client | None = None,
        async_client: httpx.AsyncClient | None = None,
        tenant_policy_engine: TenantPolicyEngine | None = None,
        telemetry_sink: TelemetrySink | None = None,
        serializer: Callable[[SearchDocument], dict[str, Any]] | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._async_client = async_client
        self._tenant_policy_engine = tenant_policy_engine or TenantPolicyEngine()
        self._telemetry_sink = telemetry_sink or NoopTelemetrySink()
        self._serializer = serializer or self._default_document_body

    def publish(self, document: SearchDocument, *, trace_id: str | None = None) -> dict[str, Any]:
        self._ensure_sync_context("publish_async")
        return self._publish_sync(document=document, trace_id=trace_id)

    async def publish_async(
        self,
        document: SearchDocument,
        *,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._publish_async(document=document, trace_id=trace_id)

    def _publish_sync(self, *, document: SearchDocument, trace_id: str | None) -> dict[str, Any]:
        alias, routing = self._config.resolve_write_alias(document, self._tenant_policy_engine)
        params = self._request_params(document=document, routing=routing)
        headers = self._headers(trace_id=trace_id)
        path = f"/{quote(alias, safe='')}/_doc/{quote(document.document_id, safe='')}"
        with self._telemetry_sink.start_span(
            "projection.publisher.publish",
            trace_id=trace_id,
            attributes={
                "tenant_id": document.tenant_id,
                "domain_name": document.domain_name,
                "entity_type": document.entity_type,
            },
        ):
            client = self._get_sync_client()
            if document.completeness_status == CompletenessStatus.DELETED:
                response = client.delete(self._build_url(path), params=params, headers=headers)
                action = "deleted"
            else:
                body = self._serializer(document)
                response = client.put(self._build_url(path), params=params, headers=headers, json=body)
                action = "indexed"
            response.raise_for_status()
            payload = response.json()
            self._telemetry_sink.increment(
                f"projection.publisher.{action}",
                tags={"entity_type": document.entity_type, "domain_name": document.domain_name},
            )
            self._telemetry_sink.emit(
                TelemetryEvent(
                    event_type=f"projection_publisher_{action}",
                    trace_id=trace_id,
                    attributes={
                        "tenant_id": document.tenant_id,
                        "document_id": document.document_id,
                        "entity_type": document.entity_type,
                        "projection_version": document.projection_version,
                    },
                )
            )
            return {
                "result": payload.get("result"),
                "status": response.status_code,
                "alias": alias,
                "routing": routing,
                "raw": payload,
            }

    async def _publish_async(self, *, document: SearchDocument, trace_id: str | None) -> dict[str, Any]:
        alias, routing = self._config.resolve_write_alias(document, self._tenant_policy_engine)
        params = self._request_params(document=document, routing=routing)
        headers = self._headers(trace_id=trace_id)
        path = f"/{quote(alias, safe='')}/_doc/{quote(document.document_id, safe='')}"
        with self._telemetry_sink.start_span(
            "projection.publisher.publish",
            trace_id=trace_id,
            attributes={
                "tenant_id": document.tenant_id,
                "domain_name": document.domain_name,
                "entity_type": document.entity_type,
            },
        ):
            client = self._get_async_client()
            if document.completeness_status == CompletenessStatus.DELETED:
                response = await client.delete(self._build_url(path), params=params, headers=headers)
                action = "deleted"
            else:
                body = self._serializer(document)
                response = await client.put(
                    self._build_url(path),
                    params=params,
                    headers=headers,
                    json=body,
                )
                action = "indexed"
            response.raise_for_status()
            payload = response.json()
            self._telemetry_sink.increment(
                f"projection.publisher.{action}",
                tags={"entity_type": document.entity_type, "domain_name": document.domain_name},
            )
            self._telemetry_sink.emit(
                TelemetryEvent(
                    event_type=f"projection_publisher_{action}",
                    trace_id=trace_id,
                    attributes={
                        "tenant_id": document.tenant_id,
                        "document_id": document.document_id,
                        "entity_type": document.entity_type,
                        "projection_version": document.projection_version,
                    },
                )
            )
            return {
                "result": payload.get("result"),
                "status": response.status_code,
                "alias": alias,
                "routing": routing,
                "raw": payload,
            }

    def publish_many(self, documents: list[SearchDocument], *, trace_id: str | None = None) -> dict[str, Any]:
        self._ensure_sync_context("publish_many_async")
        return self._publish_many_sync(documents=documents, trace_id=trace_id)

    async def publish_many_async(
        self,
        documents: list[SearchDocument],
        *,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._publish_many_async(documents=documents, trace_id=trace_id)

    def _publish_many_sync(self, *, documents: list[SearchDocument], trace_id: str | None) -> dict[str, Any]:
        if not documents:
            return {"items": [], "errors": False}

        lines: list[str] = []
        document_ids: list[str] = []
        for document in documents:
            alias, routing = self._config.resolve_write_alias(document, self._tenant_policy_engine)
            operation = "delete" if document.completeness_status == CompletenessStatus.DELETED else "index"
            metadata: dict[str, Any] = {
                "_index": alias,
                "_id": document.document_id,
                "version": document.projection_version,
                "version_type": "external",
            }
            if routing is not None:
                metadata["routing"] = routing
            lines.append(json.dumps({operation: metadata}, separators=(",", ":")))
            if operation == "index":
                lines.append(json.dumps(self._serializer(document), default=str, separators=(",", ":")))
            document_ids.append(document.document_id)

        response = self._get_sync_client().post(
            self._build_url("/_bulk"),
            headers={**self._headers(trace_id=trace_id), "Content-Type": "application/x-ndjson"},
            params={"refresh": self._config.refresh} if self._config.refresh is not None else None,
            content="\n".join(lines) + "\n",
        )
        response.raise_for_status()
        payload = response.json()
        self._telemetry_sink.increment("projection.publisher.bulk_requests")
        return self._normalize_bulk_response(payload=payload, document_ids=document_ids, trace_id=trace_id)

    async def _publish_many_async(
        self,
        *,
        documents: list[SearchDocument],
        trace_id: str | None,
    ) -> dict[str, Any]:
        if not documents:
            return {"items": [], "errors": False}

        lines: list[str] = []
        document_ids: list[str] = []
        for document in documents:
            alias, routing = self._config.resolve_write_alias(document, self._tenant_policy_engine)
            operation = "delete" if document.completeness_status == CompletenessStatus.DELETED else "index"
            metadata: dict[str, Any] = {
                "_index": alias,
                "_id": document.document_id,
                "version": document.projection_version,
                "version_type": "external",
            }
            if routing is not None:
                metadata["routing"] = routing
            lines.append(json.dumps({operation: metadata}, separators=(",", ":")))
            if operation == "index":
                lines.append(json.dumps(self._serializer(document), default=str, separators=(",", ":")))
            document_ids.append(document.document_id)

        response = await self._get_async_client().post(
            self._build_url("/_bulk"),
            headers={**self._headers(trace_id=trace_id), "Content-Type": "application/x-ndjson"},
            params={"refresh": self._config.refresh} if self._config.refresh is not None else None,
            content="\n".join(lines) + "\n",
        )
        response.raise_for_status()
        payload = response.json()
        self._telemetry_sink.increment("projection.publisher.bulk_requests")
        return self._normalize_bulk_response(payload=payload, document_ids=document_ids, trace_id=trace_id)

    def close(self) -> None:
        self._ensure_sync_context("aclose")
        if self._client is not None:
            self._client.close()
        if self._async_client is not None:
            asyncio.run(self._async_client.aclose())

    async def aclose(self) -> None:
        if self._async_client is not None:
            await self._async_client.aclose()
        if self._client is not None:
            self._client.close()

    def _normalize_bulk_response(
        self,
        *,
        payload: dict[str, Any],
        document_ids: list[str],
        trace_id: str | None,
    ) -> dict[str, Any]:
        items = payload.get("items", [])
        failed_items: list[dict[str, Any]] = []
        failed_document_ids: list[str] = []
        if isinstance(items, list):
            for index, item in enumerate(items):
                if not isinstance(item, dict) or not item:
                    continue
                operation, details = next(iter(item.items()))
                if not isinstance(details, dict):
                    continue
                error = details.get("error")
                if error is None:
                    continue
                document_id = str(details.get("_id", document_ids[index] if index < len(document_ids) else ""))
                failed_document_ids.append(document_id)
                failed_items.append(
                    {
                        "operation": operation,
                        "document_id": document_id,
                        "status": details.get("status"),
                        "error": error,
                    }
                )
        if failed_items:
            self._telemetry_sink.increment("projection.publisher.bulk_failures", value=len(failed_items))
            self._telemetry_sink.emit(
                TelemetryEvent(
                    event_type="projection_publisher_bulk_failures",
                    severity="warning",
                    trace_id=trace_id,
                    attributes={
                        "failed_document_ids": failed_document_ids,
                        "failure_count": len(failed_items),
                    },
                )
            )
        return {
            "errors": bool(payload.get("errors", False)),
            "items": items if isinstance(items, list) else [],
            "failed_items": failed_items,
            "failed_document_ids": failed_document_ids,
            "raw": payload,
        }

    def _request_params(self, *, document: SearchDocument, routing: str | None) -> dict[str, str]:
        params = {
            "version": str(document.projection_version),
            "version_type": "external",
        }
        if routing is not None:
            params["routing"] = routing
        if self._config.refresh is not None:
            params["refresh"] = self._config.refresh
        return params

    def _headers(self, *, trace_id: str | None) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"ApiKey {self._config.api_key}"
        elif self._config.bearer_token:
            headers["Authorization"] = f"Bearer {self._config.bearer_token}"
        if trace_id:
            headers["X-Opaque-Id"] = trace_id
        return headers

    def _build_url(self, path: str) -> str:
        return f"{self._config.endpoint.rstrip('/')}{path}"

    def _get_sync_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client()
        return self._client

    def _get_async_client(self) -> httpx.AsyncClient:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient()
        return self._async_client

    @staticmethod
    def _ensure_sync_context(async_method_name: str) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise RuntimeError(f"{async_method_name} must be used when publishing from an async context")

    @staticmethod
    def _default_document_body(document: SearchDocument) -> dict[str, Any]:
        body = dict(document.payload)
        body.setdefault("id", document.document_id)
        body.setdefault("tenant_id", document.tenant_id)
        body.setdefault("domain_name", document.domain_name)
        body.setdefault("entity_type", document.entity_type)
        body.setdefault("projection_version", document.projection_version)
        body.setdefault("completeness_status", document.completeness_status.value)
        body.setdefault("source_versions", document.source_versions)
        body["_meta"] = {
            "document_id": document.document_id,
            "tenant_id": document.tenant_id,
            "domain_name": document.domain_name,
            "entity_type": document.entity_type,
            "projection_version": document.projection_version,
            "completeness_status": document.completeness_status.value,
            "source_versions": document.source_versions,
        }
        return body
