from starlette.testclient import TestClient

from unified_modernization.gateway.asgi import ASGIRuntimeConfig, build_app


def test_translate_supports_compound_filters_through_asgi() -> None:
    app = build_app(
        ASGIRuntimeConfig(
            environment="test",
            field_map={"Status": "status", "Tier": "tier", "Region": "region"},
        )
    )
    client = TestClient(app)

    response = client.post(
        "/translate",
        json={"params": {"$filter": "(Status eq 'ACTIVE' and Tier gt 3) or not Region eq 'EU'"}},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["query"]["query"]["bool"]["filter"][0]["bool"]["minimum_should_match"] == 1


def test_translate_requires_api_key_in_prod() -> None:
    app = build_app(
        ASGIRuntimeConfig(
            environment="prod",
            valid_api_keys={"secret"},
            field_map={"Status": "status"},
        )
    )
    client = TestClient(app)

    unauthorized = client.post("/translate", json={"params": {"$filter": "Status eq 'ACTIVE'"}})
    authorized = client.post(
        "/translate",
        headers={"X-API-Key": "secret"},
        json={"params": {"$filter": "Status eq 'ACTIVE'"}},
    )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200


def test_translate_fails_closed_when_prod_api_keys_are_unconfigured() -> None:
    app = build_app(
        ASGIRuntimeConfig(
            environment="prod",
            valid_api_keys=set(),
            field_map={"Status": "status"},
        )
    )
    client = TestClient(app)

    response = client.post("/translate", json={"params": {"$filter": "Status eq 'ACTIVE'"}})

    assert response.status_code == 401


def test_translate_rejects_unknown_fields_with_422() -> None:
    app = build_app(
        ASGIRuntimeConfig(
            environment="test",
            field_map={"Status": "status"},
        )
    )
    client = TestClient(app)

    response = client.post("/translate", json={"params": {"$filter": "Tier eq 'gold'"}})

    assert response.status_code == 422
    assert response.json()["code"] == "UNSUPPORTED_ODATA"


def test_translate_rejects_large_payloads() -> None:
    app = build_app(
        ASGIRuntimeConfig(
            environment="test",
            max_body_bytes=32,
            field_map={"Status": "status"},
        )
    )
    client = TestClient(app)

    response = client.post("/translate", content=b"x" * 64, headers={"content-type": "application/json"})

    assert response.status_code == 413


def test_translate_rejects_invalid_json() -> None:
    app = build_app(
        ASGIRuntimeConfig(
            environment="test",
            field_map={"Status": "status"},
        )
    )
    client = TestClient(app)

    response = client.post("/translate", content=b"{not-json", headers={"content-type": "application/json"})

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_JSON"
