from starlette.testclient import TestClient

from unified_modernization.gateway.asgi import ASGIRuntimeConfig
from unified_modernization.gateway.http_api import build_http_gateway_app


class _FakeSearchGatewayService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, dict[str, str]]] = []

    async def search(
        self,
        consumer_id: str,
        tenant_id: str,
        entity_type: str,
        raw_params: dict[str, str],
    ) -> dict[str, object]:
        self.calls.append((consumer_id, tenant_id, entity_type, dict(raw_params)))
        return {"results": [{"id": "elastic-doc"}], "count": 1}


def test_http_gateway_search_route_invokes_service() -> None:
    service = _FakeSearchGatewayService()
    app = build_http_gateway_app(config=ASGIRuntimeConfig(environment="test"), service=service)
    client = TestClient(app)

    response = client.post(
        "/search",
        json={
            "consumer_id": "consumer-1",
            "tenant_id": "tenant-a",
            "entity_type": "customerDocument",
            "params": {"$search": "gold"},
        },
    )

    assert response.status_code == 200
    assert response.json()["results"][0]["id"] == "elastic-doc"
    assert service.calls == [("consumer-1", "tenant-a", "customerDocument", {"$search": "gold"})]


def test_http_gateway_search_returns_400_for_invalid_payload() -> None:
    app = build_http_gateway_app(
        config=ASGIRuntimeConfig(environment="test"),
        service=_FakeSearchGatewayService(),
    )
    client = TestClient(app)

    response = client.post(
        "/search",
        json={"tenant_id": "tenant-a", "entity_type": "customerDocument", "params": {"$search": "gold"}},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_SEARCH_REQUEST"


def test_http_gateway_search_returns_503_when_service_is_not_configured() -> None:
    app = build_http_gateway_app(config=ASGIRuntimeConfig(environment="test"))
    client = TestClient(app)

    response = client.post(
        "/search",
        json={
            "consumer_id": "consumer-1",
            "tenant_id": "tenant-a",
            "entity_type": "customerDocument",
            "params": {"$search": "gold"},
        },
    )

    assert response.status_code == 503
    assert response.json()["code"] == "SEARCH_GATEWAY_NOT_CONFIGURED"


def test_http_gateway_fails_closed_on_bootstrap_errors_in_prod() -> None:
    try:
        build_http_gateway_app(config=ASGIRuntimeConfig(environment="prod"))
    except RuntimeError as exc:
        assert "failed to initialize HTTP search gateway service" in str(exc)
    else:
        raise AssertionError("prod gateway app should fail closed when the service bootstrap is invalid")
