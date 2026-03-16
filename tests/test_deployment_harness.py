import asyncio
import json
from pathlib import Path

from unified_modernization.gateway.bootstrap import (
    TrafficMode,
    build_http_search_gateway_service_from_env,
    load_gateway_integration_config_from_env,
)
from unified_modernization.gateway.harness import (
    SearchHarnessCase,
    SearchHarnessConfig,
    load_harness_cases,
    run_search_gateway_harness,
    run_smoke_test,
)
from unified_modernization.observability.bootstrap import (
    build_telemetry_sink,
    load_telemetry_runtime_config_from_env,
)
from unified_modernization.observability.telemetry import InMemoryTelemetrySink, NoopTelemetrySink, StructuredLoggerTelemetrySink
from unified_modernization.projection.bootstrap import (
    build_elasticsearch_document_publisher_from_env,
    load_projection_publisher_runtime_config_from_env,
)


class _FakeBackend:
    def __init__(self, response: dict) -> None:
        self._response = response

    async def query(self, request: dict) -> dict:
        del request
        return self._response


def test_load_gateway_integration_config_from_env_parses_runtime_and_index_maps() -> None:
    config = load_gateway_integration_config_from_env(
        {
            "UMP_ENVIRONMENT": "prod",
            "UMP_GATEWAY_MODE": "canary",
            "UMP_GATEWAY_CANARY_PERCENT": "25",
            "UMP_AZURE_SEARCH_ENDPOINT": "https://azure.example.com",
            "UMP_AZURE_SEARCH_DEFAULT_INDEX": "azure-customers",
            "UMP_AZURE_SEARCH_INDEX_MAP": "invoiceDocument=azure-invoices",
            "UMP_ELASTICSEARCH_ENDPOINT": "https://elastic.example.com",
            "UMP_ELASTICSEARCH_DEFAULT_INDEX": "elastic-customers",
            "UMP_ELASTICSEARCH_INDEX_MAP": "invoiceDocument=elastic-invoices",
            "UMP_GATEWAY_FIELD_MAP": "Status=status,Tier=tier",
            "UMP_DEDICATED_TENANTS": "tenant-a,tenant-b",
        }
    )

    assert config.runtime.environment == "prod"
    assert config.runtime.mode == TrafficMode.CANARY
    assert config.runtime.canary_percent == 25
    assert config.azure.index_names_by_entity_type["invoiceDocument"] == "azure-invoices"
    assert config.elastic.index_names_by_entity_type["invoiceDocument"] == "elastic-invoices"
    assert config.field_map["Status"] == "status"
    assert config.dedicated_tenants == {"tenant-a", "tenant-b"}


def test_load_projection_publisher_runtime_config_from_env_parses_alias_map() -> None:
    config = load_projection_publisher_runtime_config_from_env(
        {
            "UMP_PUBLISHER_ENDPOINT": "https://elastic.example.com",
            "UMP_PUBLISHER_WRITE_ALIAS_MAP": "customerDocument=customer-write,invoiceDocument=invoice-write",
            "UMP_DEDICATED_TENANTS": "tenant-a",
        }
    )

    assert config.endpoint == "https://elastic.example.com"
    assert config.write_aliases_by_entity_type["customerDocument"] == "customer-write"
    assert config.dedicated_tenants == {"tenant-a"}


def test_build_elasticsearch_document_publisher_from_env_uses_config_values() -> None:
    publisher = build_elasticsearch_document_publisher_from_env(
        env={
            "UMP_PUBLISHER_ENDPOINT": "https://elastic.example.com",
            "UMP_PUBLISHER_API_KEY": "secret",
            "UMP_DEDICATED_TENANTS": "tenant-a",
        }
    )

    assert publisher._config.endpoint == "https://elastic.example.com"
    assert publisher._config.api_key == "secret"
    assert publisher._tenant_policy_engine.resolve("tenant-a", "customerDocument").write_alias == "customerDocument-tenant-a-write"


def test_build_telemetry_sink_from_env_supports_memory_and_logger() -> None:
    memory_sink = build_telemetry_sink(
        load_telemetry_runtime_config_from_env({"UMP_TELEMETRY_MODE": "memory"})
    )
    logger_sink = build_telemetry_sink(
        load_telemetry_runtime_config_from_env({"UMP_TELEMETRY_MODE": "logger"})
    )
    noop_sink = build_telemetry_sink(
        load_telemetry_runtime_config_from_env({"UMP_TELEMETRY_MODE": "noop"})
    )

    assert isinstance(memory_sink, InMemoryTelemetrySink)
    assert isinstance(logger_sink, StructuredLoggerTelemetrySink)
    assert isinstance(noop_sink, NoopTelemetrySink)


def test_build_http_search_gateway_service_from_env_constructs_real_clients() -> None:
    service = build_http_search_gateway_service_from_env(
        env={
            "UMP_ENVIRONMENT": "dev",
            "UMP_AZURE_SEARCH_ENDPOINT": "https://azure.example.com",
            "UMP_AZURE_SEARCH_DEFAULT_INDEX": "customers",
            "UMP_ELASTICSEARCH_ENDPOINT": "https://elastic.example.com",
            "UMP_ELASTICSEARCH_DEFAULT_INDEX": "customers-shared-a-read",
            "UMP_GATEWAY_FIELD_MAP": "Status=status",
        },
        telemetry_sink=InMemoryTelemetrySink(),
    )

    assert service._translator.translate({"$filter": "Status eq 'ACTIVE'"})["query"]["bool"]["filter"][0] == {
        "term": {"status": "ACTIVE"}
    }
    assert service._mode == TrafficMode.AZURE_ONLY


def test_load_harness_cases_supports_json_and_jsonl(tmp_path: Path) -> None:
    json_path = tmp_path / "cases.json"
    jsonl_path = tmp_path / "cases.jsonl"
    payload = [
        {
            "name": "customer-search",
            "consumer_id": "consumer-1",
            "tenant_id": "tenant-a",
            "entity_type": "customerDocument",
            "raw_params": {"$search": "gold"},
        }
    ]
    json_path.write_text(json.dumps(payload), encoding="utf-8")
    jsonl_path.write_text("\n".join(json.dumps(item) for item in payload) + "\n", encoding="utf-8")

    assert load_harness_cases(json_path)[0].name == "customer-search"
    assert load_harness_cases(jsonl_path)[0].tenant_id == "tenant-a"


def test_search_gateway_harness_reports_latency_and_shadow_counts() -> None:
    telemetry = InMemoryTelemetrySink()
    from unified_modernization.gateway.service import SearchGatewayService, TrafficMode

    service = SearchGatewayService(
        azure_backend=_FakeBackend({"results": [{"id": "1"}, {"id": "2"}]}),
        elastic_backend=_FakeBackend({"results": [{"id": "2"}, {"id": "3"}]}),
        mode=TrafficMode.SHADOW,
        telemetry_sink=telemetry,
    )

    report = asyncio.run(
        run_search_gateway_harness(
            service,
            [
                SearchHarnessCase(
                    name="customer-search",
                    consumer_id="consumer-1",
                    tenant_id="tenant-a",
                    entity_type="customerDocument",
                    raw_params={"$search": "gold"},
                    weight=2,
                )
            ],
            config=SearchHarnessConfig(concurrency=2, iterations=2),
            telemetry_sink=telemetry,
        )
    )

    assert report.total_requests == 4
    assert report.failed_requests == 0
    assert report.successful_requests == 4
    assert report.shadow_regressions == 0
    assert report.average_latency_ms >= 0.0


def test_run_smoke_test_executes_single_pass() -> None:
    telemetry = InMemoryTelemetrySink()
    from unified_modernization.gateway.service import SearchGatewayService, TrafficMode

    service = SearchGatewayService(
        azure_backend=_FakeBackend({"results": [{"id": "1"}]}),
        elastic_backend=_FakeBackend({"results": [{"id": "1"}]}),
        mode=TrafficMode.SHADOW,
        telemetry_sink=telemetry,
    )

    report = asyncio.run(
        run_smoke_test(
            service,
            [
                SearchHarnessCase(
                    name="smoke",
                    consumer_id="consumer-1",
                    tenant_id="tenant-a",
                    entity_type="customerDocument",
                    raw_params={"$search": "gold"},
                )
            ],
            telemetry_sink=telemetry,
        )
    )

    assert report.total_requests == 1
    assert report.failed_requests == 0
