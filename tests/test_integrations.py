import asyncio
import json
from datetime import UTC, datetime

import httpx

from unified_modernization.adapters.cosmos_change_feed import CosmosChangeFeedAdapter, CosmosChangeFeedAdapterConfig
from unified_modernization.adapters.debezium_cdc import DebeziumChangeEventAdapter, DebeziumChangeEventAdapterConfig
from unified_modernization.adapters.spanner_change_stream import SpannerChangeStreamAdapter, SpannerChangeStreamAdapterConfig
from unified_modernization.contracts.events import ChangeType, MigrationStage, SourceTechnology
from unified_modernization.contracts.projection import CompletenessStatus, DependencyPolicy, DependencyRule, SearchDocument
from unified_modernization.gateway.bootstrap import (
    AzureGatewayBackendConfig,
    ElasticsearchGatewayBackendConfig,
    GatewayIntegrationConfig,
    GatewayRuntimeConfig,
    build_http_search_gateway_service,
)
from unified_modernization.gateway.clients import (
    AzureAISearchBackend,
    AzureSearchBackendConfig,
    ElasticsearchBackendConfig,
    ElasticsearchSearchBackend,
)
from unified_modernization.observability.telemetry import InMemoryTelemetrySink
from unified_modernization.projection.builder import ProjectionBuilder
from unified_modernization.projection.bootstrap import (
    ProjectionPublisherRuntimeConfig,
    build_elasticsearch_document_publisher,
)
from unified_modernization.projection.publisher import ElasticsearchDocumentPublisher, ElasticsearchPublisherConfig
from unified_modernization.projection.runtime import InMemoryDeadLetterQueue, ProjectionRuntime
from unified_modernization.projection.store import InMemoryProjectionStateStore
from unified_modernization.routing.tenant_policy import TenantPolicyEngine


def test_azure_ai_search_backend_shapes_request_and_normalizes_response() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "@odata.count": 1,
                "@search.facets": {"status": [{"value": "ACTIVE", "count": 1}]},
                "value": [{"docKey": "doc-1", "title": "Gold customer"}],
            },
        )

    async def run_test() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = AzureAISearchBackend(
                AzureSearchBackendConfig(
                    endpoint="https://example.search.windows.net",
                    default_index_name="customers",
                    api_key="azure-key",
                    document_id_field="docKey",
                ),
                client=client,
            )
            return await backend.query(
                {
                    "entity_type": "customerDocument",
                    "trace_id": "trace-1",
                    "params": {
                        "$search": "gold customer",
                        "$filter": "Status eq 'ACTIVE'",
                        "$top": "10",
                        "$count": "true",
                    },
                }
            )

    response = asyncio.run(run_test())

    assert captured["method"] == "POST"
    assert "indexes/customers/docs/search" in str(captured["url"])
    assert "api-version=2025-09-01" in str(captured["url"])
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["api-key"] == "azure-key"
    assert headers["x-ms-client-request-id"] == "trace-1"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["search"] == "gold customer"
    assert body["filter"] == "Status eq 'ACTIVE'"
    assert body["count"] is True
    assert response["results"][0]["id"] == "doc-1"
    assert response["count"] == 1


def test_elasticsearch_search_backend_uses_alias_and_routing() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "hits": {
                    "total": {"value": 1},
                    "hits": [
                        {
                            "_id": "doc-1",
                            "_score": 1.2,
                            "_source": {"title": "Gold customer"},
                        }
                    ],
                },
                "aggregations": {"status": {"buckets": [{"key": "ACTIVE", "doc_count": 1}]}},
            },
        )

    async def run_test() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = ElasticsearchSearchBackend(
                ElasticsearchBackendConfig(
                    endpoint="https://elastic.example.com",
                    api_key="elastic-key",
                ),
                client=client,
            )
            return await backend.query(
                {
                    "alias": "customerDocument-shared_a-read",
                    "entity_type": "customerDocument",
                    "routing": "tenant-a",
                    "trace_id": "trace-2",
                    "query": {"query": {"match": {"title": "gold"}}},
                }
            )

    response = asyncio.run(run_test())

    assert captured["method"] == "POST"
    assert "customerDocument-shared_a-read/_search" in str(captured["url"])
    assert "routing=tenant-a" in str(captured["url"])
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["authorization"] == "ApiKey elastic-key"
    assert headers["x-opaque-id"] == "trace-2"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["track_total_hits"] is True
    assert response["results"][0]["id"] == "doc-1"
    assert response["count"] == 1


def test_elasticsearch_document_publisher_applies_external_versioning() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(201, json={"result": "created"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    publisher = ElasticsearchDocumentPublisher(
        ElasticsearchPublisherConfig(endpoint="https://elastic.example.com", api_key="elastic-key"),
        client=client,
        tenant_policy_engine=TenantPolicyEngine(dedicated_tenants={"tenant-a"}),
        telemetry_sink=InMemoryTelemetrySink(),
    )

    result = publisher.publish(
        SearchDocument(
            document_id="doc-1",
            tenant_id="tenant-a",
            domain_name="customer_documents",
            entity_type="customerDocument",
            projection_version=7,
            completeness_status=CompletenessStatus.COMPLETE,
            source_versions={"document_core": 12},
            payload={"title": "Gold customer"},
        ),
        trace_id="trace-3",
    )

    client.close()

    assert captured["method"] == "PUT"
    assert "customerDocument-tenant-a-write/_doc/doc-1" in str(captured["url"])
    assert "version=7" in str(captured["url"])
    assert "version_type=external" in str(captured["url"])
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["authorization"] == "ApiKey elastic-key"
    assert headers["x-opaque-id"] == "trace-3"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["id"] == "doc-1"
    assert body["projection_version"] == 7
    assert result["result"] == "created"


def test_elasticsearch_document_publisher_deletes_with_external_versioning() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"result": "deleted"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    publisher = ElasticsearchDocumentPublisher(
        ElasticsearchPublisherConfig(endpoint="https://elastic.example.com"),
        client=client,
    )

    result = publisher.publish(
        SearchDocument(
            document_id="doc-9",
            tenant_id="tenant-a",
            domain_name="customer_documents",
            entity_type="customerDocument",
            projection_version=11,
            completeness_status=CompletenessStatus.DELETED,
            source_versions={"document_core": 14},
            payload={"is_deleted": True},
        )
    )

    client.close()

    assert captured["method"] == "DELETE"
    assert "version=11" in str(captured["url"])
    assert result["result"] == "deleted"


def test_projection_runtime_publishes_documents_when_publisher_is_configured() -> None:
    class _CapturingPublisher:
        def __init__(self) -> None:
            self.published: list[SearchDocument] = []

        def publish(self, document: SearchDocument, *, trace_id: str | None = None) -> dict[str, object]:
            del trace_id
            self.published.append(document)
            return {"result": "created"}

    builder = ProjectionBuilder(
        [
            DependencyPolicy(
                domain_name="customer_documents",
                entity_type="customerDocument",
                rules=[DependencyRule(owner="document_core", required=True)],
            )
        ],
        state_store=InMemoryProjectionStateStore(),
    )
    publisher = _CapturingPublisher()
    runtime = ProjectionRuntime(builder, document_publisher=publisher)

    result = runtime.process(
        CosmosChangeFeedAdapter(
            CosmosChangeFeedAdapterConfig(
                domain_name="customer_documents",
                entity_type="customerDocument",
                fragment_owner="document_core",
            )
        ).normalize(
            {
                "id": "doc-4",
                "tenantId": "tenant-a",
                "_lsn": 3,
                "_ts": int(datetime.now(UTC).timestamp()),
                "title": "Published by runtime",
            }
        )
    )

    assert result.accepted is True
    assert result.publish_result == {"result": "created"}
    assert publisher.published[0].document_id == "doc-4"


def test_http_gateway_bootstrap_builds_concrete_clients() -> None:
    service = build_http_search_gateway_service(
        config=GatewayIntegrationConfig(
            runtime=GatewayRuntimeConfig(environment="dev"),
            azure=AzureGatewayBackendConfig(
                endpoint="https://example.search.windows.net",
                default_index_name="customers",
            ),
            elastic=ElasticsearchGatewayBackendConfig(
                endpoint="https://elastic.example.com",
                default_index_name="customers-read",
            ),
            field_map={"Status": "status"},
            dedicated_tenants={"tenant-a"},
        ),
    )

    assert isinstance(service._azure._backend, AzureAISearchBackend)
    assert isinstance(service._elastic._backend, ElasticsearchSearchBackend)
    assert service._translator.translate({"$filter": "Status eq 'ACTIVE'"})["query"]["bool"]["filter"][0] == {
        "term": {"status": "ACTIVE"}
    }


def test_projection_publisher_bootstrap_builds_tenant_routed_publisher() -> None:
    publisher = build_elasticsearch_document_publisher(
        config=ProjectionPublisherRuntimeConfig(
            endpoint="https://elastic.example.com",
            api_key="elastic-key",
            dedicated_tenants={"tenant-a"},
        )
    )

    assert publisher._config.endpoint == "https://elastic.example.com"
    assert publisher._tenant_policy_engine.resolve("tenant-a", "customerDocument").write_alias == "customerDocument-tenant-a-write"


def test_projection_runtime_dead_letters_publish_failures() -> None:
    class _FailingPublisher:
        def publish(self, document: SearchDocument, *, trace_id: str | None = None) -> dict[str, object]:
            del document, trace_id
            raise RuntimeError("indexing failed")

    builder = ProjectionBuilder(
        [
            DependencyPolicy(
                domain_name="customer_documents",
                entity_type="customerDocument",
                rules=[DependencyRule(owner="document_core", required=True)],
            )
        ],
        state_store=InMemoryProjectionStateStore(),
    )
    dead_letter_queue = InMemoryDeadLetterQueue()
    runtime = ProjectionRuntime(
        builder,
        dead_letter_queue=dead_letter_queue,
        document_publisher=_FailingPublisher(),
    )

    result = runtime.process(
        CosmosChangeFeedAdapter(
            CosmosChangeFeedAdapterConfig(
                domain_name="customer_documents",
                entity_type="customerDocument",
                fragment_owner="document_core",
            )
        ).normalize(
            {
                "id": "doc-5",
                "tenantId": "tenant-a",
                "_lsn": 4,
                "_ts": int(datetime.now(UTC).timestamp()),
                "title": "Fails publish",
            }
        )
    )

    assert result.accepted is False
    assert result.dead_lettered is True
    assert result.reason_code == "publish_failed"
    assert dead_letter_queue.records[0].logical_entity_id == "doc-5"


def test_cosmos_change_feed_adapter_normalizes_delete_record() -> None:
    adapter = CosmosChangeFeedAdapter(
        CosmosChangeFeedAdapterConfig(
            domain_name="customer_documents",
            entity_type="customerDocument",
            fragment_owner="document_core",
        )
    )

    event = adapter.normalize(
        {
            "id": "doc-6",
            "tenantId": "tenant-a",
            "_lsn": "42",
            "_ts": 1_710_000_000,
            "operationType": "delete",
            "title": "Deleted",
        }
    )

    assert event.source_technology == SourceTechnology.COSMOS
    assert event.change_type == ChangeType.DELETE
    assert event.logical_entity_id == "doc-6"
    assert event.source_version == 42


def test_debezium_change_event_adapter_normalizes_upsert() -> None:
    adapter = DebeziumChangeEventAdapter(
        DebeziumChangeEventAdapterConfig(
            domain_name="customer_documents",
            entity_type="customerDocument",
            fragment_owner="customer_profile",
            source_technology=SourceTechnology.AZURE_SQL,
            migration_stage=MigrationStage.GCP_SHADOW_VALIDATED,
        )
    )

    event = adapter.normalize(
        {
            "payload": {
                "op": "u",
                "ts_ms": 1_710_000_000_000,
                "source": {"lsn": 12345},
                "after": {
                    "id": "doc-7",
                    "tenant_id": "tenant-a",
                    "customerName": "Apurva",
                },
            }
        }
    )

    assert event.source_technology == SourceTechnology.AZURE_SQL
    assert event.change_type == ChangeType.UPSERT
    assert event.source_version == 12345
    assert event.migration_stage == MigrationStage.GCP_SHADOW_VALIDATED
    assert event.payload["customerName"] == "Apurva"


def test_debezium_change_event_adapter_uses_before_image_for_deletes() -> None:
    adapter = DebeziumChangeEventAdapter(
        DebeziumChangeEventAdapterConfig(
            domain_name="customer_documents",
            entity_type="customerDocument",
            fragment_owner="customer_profile",
            source_technology=SourceTechnology.ALLOYDB,
        )
    )

    event = adapter.normalize(
        {
            "payload": {
                "op": "d",
                "ts_ms": "1710000000000",
                "source": {"lsn": "16:123:9"},
                "before": {
                    "id": "doc-8",
                    "tenant_id": "tenant-b",
                    "customerName": "Deleted Customer",
                },
            }
        }
    )

    assert event.source_technology == SourceTechnology.ALLOYDB
    assert event.change_type == ChangeType.DELETE
    assert event.logical_entity_id == "doc-8"
    assert event.source_version == 161239


def test_spanner_change_stream_adapter_normalizes_update_record() -> None:
    adapter = SpannerChangeStreamAdapter(
        SpannerChangeStreamAdapterConfig(
            domain_name="customer_documents",
            entity_type="customerDocument",
            fragment_owner="document_core",
        )
    )

    event = adapter.normalize(
        {
            "mod_type": "UPDATE",
            "record_sequence": "00000000000000012345",
            "commit_timestamp": "2026-03-16T12:00:00Z",
            "keys": {"id": "doc-9", "tenant_id": "tenant-a"},
            "new_values": {
                "id": "doc-9",
                "tenant_id": "tenant-a",
                "title": "From Spanner",
            },
        }
    )

    assert event.source_technology == SourceTechnology.SPANNER
    assert event.change_type == ChangeType.UPSERT
    assert event.source_version == 12345
    assert event.payload["title"] == "From Spanner"
