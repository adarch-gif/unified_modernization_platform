from __future__ import annotations

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
        tenant_policy_engine: TenantPolicyEngine | None = None,
        telemetry_sink: TelemetrySink | None = None,
        serializer: Callable[[SearchDocument], dict[str, Any]] | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.Client()
        self._tenant_policy_engine = tenant_policy_engine or TenantPolicyEngine()
        self._telemetry_sink = telemetry_sink or NoopTelemetrySink()
        self._serializer = serializer or self._default_document_body

    def publish(self, document: SearchDocument, *, trace_id: str | None = None) -> dict[str, Any]:
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
            if document.completeness_status == CompletenessStatus.DELETED:
                response = self._client.delete(self._build_url(path), params=params, headers=headers)
                action = "deleted"
            else:
                body = self._serializer(document)
                response = self._client.put(self._build_url(path), params=params, headers=headers, json=body)
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
        if not documents:
            return {"items": [], "errors": False}

        lines: list[str] = []
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

        response = self._client.post(
            self._build_url("/_bulk"),
            headers={**self._headers(trace_id=trace_id), "Content-Type": "application/x-ndjson"},
            params={"refresh": self._config.refresh} if self._config.refresh is not None else None,
            content="\n".join(lines) + "\n",
        )
        response.raise_for_status()
        payload = response.json()
        self._telemetry_sink.increment("projection.publisher.bulk_requests")
        return {
            "errors": bool(payload.get("errors", False)),
            "items": payload.get("items", []),
            "raw": payload,
        }

    def close(self) -> None:
        self._client.close()

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
        if self._config.bearer_token:
            headers["Authorization"] = f"Bearer {self._config.bearer_token}"
        if trace_id:
            headers["X-Opaque-Id"] = trace_id
        return headers

    def _build_url(self, path: str) -> str:
        return f"{self._config.endpoint.rstrip('/')}{path}"

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
