import asyncio

import pytest

from unified_modernization.gateway.bootstrap import GatewayRuntimeConfig, build_search_gateway_service
from unified_modernization.gateway.evaluation import (
    QueryEvaluationCase,
    QueryJudgment,
    SearchEvaluationHarness,
    ShadowQualityGate,
)
from unified_modernization.gateway.odata import ODataTranslator
from unified_modernization.gateway.resilience import CircuitOpenError, ResilientSearchBackend
from unified_modernization.gateway.service import SearchGatewayService, TrafficMode
from unified_modernization.observability.telemetry import InMemoryTelemetrySink
from unified_modernization.routing.tenant_policy import TenantPolicyEngine


def test_translate_basic_search_and_filter() -> None:
    translator = ODataTranslator({"Status": "status"})
    query = translator.translate(
        {
            "$search": "gold customer",
            "$filter": "Status eq 'ACTIVE'",
            "$top": "10",
        }
    )

    assert query["size"] == 10
    assert query["query"]["bool"]["must"][0]["multi_match"]["query"] == "gold customer"
    assert query["query"]["bool"]["filter"][0] == {"term": {"status": "ACTIVE"}}


def test_translate_orderby_and_facets() -> None:
    translator = ODataTranslator({"Tier": "tier"})
    query = translator.translate(
        {
            "$orderby": "Tier desc",
            "$facets": "Tier",
        }
    )

    assert query["sort"] == [{"tier": {"order": "desc"}}]
    assert query["aggs"]["tier"]["terms"]["field"] == "tier"


class _FakeBackend:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.last_request: dict | None = None

    async def query(self, request: dict) -> dict:
        self.last_request = request
        return self.response


class _FlakyBackend:
    def __init__(self, responses: list[dict] | None = None, failures: int = 0) -> None:
        self._responses = responses or [{"results": [{"id": "ok"}]}]
        self._failures = failures
        self.calls = 0

    async def query(self, request: dict) -> dict:
        del request
        self.calls += 1
        if self.calls <= self._failures:
            raise TimeoutError("backend timeout")
        index = min(self.calls - self._failures - 1, len(self._responses) - 1)
        return self._responses[index]


class _StaticJudgmentProvider:
    def __init__(self, judgment: QueryJudgment) -> None:
        self._judgment = judgment

    def get_judgment(
        self,
        *,
        tenant_id: str,
        entity_type: str,
        raw_params: dict[str, str],
        primary: dict,
        shadow: dict,
    ) -> QueryJudgment | None:
        del tenant_id, entity_type, raw_params, primary, shadow
        return self._judgment


def test_gateway_routes_tenant_to_read_alias() -> None:
    azure = _FakeBackend({"results": [{"id": "1"}]})
    elastic = _FakeBackend({"results": [{"id": "1"}]})
    service = SearchGatewayService(
        azure_backend=azure,
        elastic_backend=elastic,
        mode=TrafficMode.ELASTIC_ONLY,
        tenant_policy_engine=TenantPolicyEngine(dedicated_tenants={"tenant-a"}),
    )

    response = asyncio.run(
        service.search(
            consumer_id="consumer-1",
            tenant_id="tenant-a",
            entity_type="customerDocument",
            raw_params={"$search": "gold"},
        )
    )

    assert response["results"][0]["id"] == "1"
    assert elastic.last_request is not None
    assert elastic.last_request["alias"] == "customerDocument-tenant-a-read"
    assert elastic.last_request["routing"] is None


def test_shadow_comparison_uses_live_overlap_metrics() -> None:
    azure = _FakeBackend({"results": [{"id": "1"}, {"id": "2"}]})
    elastic = _FakeBackend({"results": [{"id": "2"}, {"id": "3"}]})
    service = SearchGatewayService(
        azure_backend=azure,
        elastic_backend=elastic,
        mode=TrafficMode.SHADOW,
    )

    asyncio.run(
        service.search(
            consumer_id="consumer-1",
            tenant_id="tenant-a",
            entity_type="customerDocument",
            raw_params={"$search": "gold"},
        )
    )

    assert service.last_shadow_comparison is not None
    assert service.last_shadow_comparison["overlap_at_k"] == 1
    assert service.last_shadow_comparison["identical_order"] is False


def test_evaluation_harness_calculates_ndcg_and_mrr() -> None:
    harness = SearchEvaluationHarness()
    case = QueryEvaluationCase(
        query_id="q1",
        retrieved_ids=["doc-2", "doc-1", "doc-3"],
        judgment=QueryJudgment(query_id="q1", graded_relevance={"doc-1": 3.0, "doc-2": 2.0}),
    )

    report = harness.evaluate_corpus([case])

    assert report.query_count == 1
    assert report.average_ndcg_at_10 > 0.7
    assert report.average_mrr == 1.0


def test_shadow_quality_gate_emits_relevance_regression_event() -> None:
    azure = _FakeBackend({"results": [{"id": "doc-1"}, {"id": "doc-2"}]})
    elastic = _FakeBackend({"results": [{"id": "doc-x"}, {"id": "doc-1"}]})
    telemetry = InMemoryTelemetrySink()
    service = SearchGatewayService(
        azure_backend=azure,
        elastic_backend=elastic,
        mode=TrafficMode.SHADOW,
        quality_gate=ShadowQualityGate(min_shadow_ndcg_at_10=0.85, max_ndcg_drop=0.10),
        judgment_provider=_StaticJudgmentProvider(
            QueryJudgment(query_id="q1", graded_relevance={"doc-1": 3.0, "doc-2": 2.0})
        ),
        telemetry_sink=telemetry,
    )

    asyncio.run(
        service.search(
            consumer_id="consumer-1",
            tenant_id="tenant-a",
            entity_type="customerDocument",
            raw_params={"$search": "gold", "query_id": "q1"},
        )
    )

    assert service.last_shadow_quality_gate is not None
    assert service.last_shadow_quality_gate["event"]["code"] == "shadow_relevance_regression"
    assert any(event["attributes"]["event"]["code"] == "shadow_relevance_regression" for event in telemetry.events if "event" in event["attributes"])


def test_resilient_backend_retries_and_recovers() -> None:
    wrapped = ResilientSearchBackend(
        _FlakyBackend(failures=1),
        name="elastic",
        timeout_seconds=0.1,
        max_retries=1,
        failure_threshold=3,
    )

    response = asyncio.run(wrapped.query({"query": {"match_all": {}}}))

    assert response["results"][0]["id"] == "ok"
    assert wrapped.state == "closed"


def test_resilient_backend_opens_circuit_after_threshold() -> None:
    telemetry = InMemoryTelemetrySink()
    wrapped = ResilientSearchBackend(
        _FlakyBackend(failures=10),
        name="azure",
        timeout_seconds=0.1,
        max_retries=0,
        failure_threshold=1,
        recovery_timeout_seconds=60.0,
        telemetry_sink=telemetry,
    )

    with pytest.raises(TimeoutError):
        asyncio.run(wrapped.query({"params": {}}))
    with pytest.raises(CircuitOpenError):
        asyncio.run(wrapped.query({"params": {}}))

    assert wrapped.state == "open"
    assert any(event["event_type"] == "circuit_open" for event in telemetry.events)


def test_gateway_bootstrap_wraps_raw_backends_and_requires_telemetry_in_prod() -> None:
    dev_service = build_search_gateway_service(
        azure_backend=_FakeBackend({"results": [{"id": "1"}]}),
        elastic_backend=_FakeBackend({"results": [{"id": "1"}]}),
        config=GatewayRuntimeConfig(environment="dev", mode=TrafficMode.SHADOW),
    )

    assert isinstance(dev_service._azure, ResilientSearchBackend)
    assert isinstance(dev_service._elastic, ResilientSearchBackend)

    with pytest.raises(ValueError):
        build_search_gateway_service(
            azure_backend=_FakeBackend({"results": [{"id": "1"}]}),
            elastic_backend=_FakeBackend({"results": [{"id": "1"}]}),
            config=GatewayRuntimeConfig(environment="prod"),
        )


def test_gateway_emits_metrics_for_shadow_mismatch() -> None:
    telemetry = InMemoryTelemetrySink()
    service = SearchGatewayService(
        azure_backend=_FakeBackend({"results": [{"id": "1"}, {"id": "2"}]}),
        elastic_backend=_FakeBackend({"results": [{"id": "2"}, {"id": "3"}]}),
        mode=TrafficMode.SHADOW,
        telemetry_sink=telemetry,
    )

    asyncio.run(
        service.search(
            consumer_id="consumer-1",
            tenant_id="tenant-a",
            entity_type="customerDocument",
            raw_params={"$search": "gold", "trace_id": "trace-1"},
        )
    )

    assert any(event["event_type"] == "search.gateway.request.start" for event in telemetry.events)
    assert telemetry.counters[("search.shadow.order_mismatch", (("entity_type", "customerDocument"),))] == 1
