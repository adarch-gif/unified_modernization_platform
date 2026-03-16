import asyncio

from unified_modernization.gateway.evaluation import QueryEvaluationCase, QueryJudgment, SearchEvaluationHarness
from unified_modernization.gateway.odata import ODataTranslator
from unified_modernization.gateway.service import SearchGatewayService, TrafficMode
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
