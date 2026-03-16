from __future__ import annotations

import json
import logging
from http import HTTPStatus
from typing import Any, cast

from pydantic import BaseModel, Field, ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from unified_modernization.gateway.asgi import APIKeyAndBodyLimitMiddleware, ASGIRuntimeConfig, _load_runtime_config
from unified_modernization.gateway.bootstrap import build_http_search_gateway_service_from_env
from unified_modernization.gateway.odata import ODataTranslator
from unified_modernization.gateway.service import SearchGatewayService
from unified_modernization.observability.bootstrap import build_telemetry_sink, load_telemetry_runtime_config_from_env


_LOGGER = logging.getLogger("unified_modernization.gateway.http_api")
_NON_PRODUCTION_ENVIRONMENTS = {"local", "dev", "test"}


class SearchGatewayRequest(BaseModel):
    consumer_id: str
    tenant_id: str
    entity_type: str
    params: dict[str, str] = Field(default_factory=dict)


def build_http_gateway_app(
    *,
    config: ASGIRuntimeConfig | None = None,
    service: SearchGatewayService | None = None,
) -> Starlette:
    runtime = config or _load_runtime_config()
    translator = ODataTranslator(runtime.field_map)
    resolved_service = service
    service_build_error: Exception | None = None
    if resolved_service is None:
        try:
            telemetry_sink = build_telemetry_sink(load_telemetry_runtime_config_from_env())
            resolved_service = build_http_search_gateway_service_from_env(telemetry_sink=telemetry_sink)
        except Exception as exc:  # pragma: no cover - exercised through route behavior
            if runtime.environment.lower() not in _NON_PRODUCTION_ENVIRONMENTS:
                raise RuntimeError("failed to initialize HTTP search gateway service") from exc
            service_build_error = exc
            _LOGGER.exception("failed to initialize HTTP search gateway service")

    async def health(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "service": "search-gateway",
                "search_gateway_configured": resolved_service is not None,
            }
        )

    async def translate(request: Request) -> JSONResponse:
        payload = await _load_json_payload(request, runtime=runtime)
        if isinstance(payload, JSONResponse):
            return payload
        params = payload.get("params", {})
        if not isinstance(params, dict):
            return JSONResponse(
                {"error": "params must be an object", "code": "INVALID_PARAMS"},
                status_code=HTTPStatus.BAD_REQUEST,
            )
        try:
            normalized_params = {str(key): str(value) for key, value in params.items() if value is not None}
            return JSONResponse({"query": translator.translate(normalized_params)})
        except ValueError as exc:
            return JSONResponse(
                {"error": str(exc), "code": "UNSUPPORTED_ODATA"},
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        except Exception:
            _LOGGER.exception("translate error")
            return JSONResponse(
                {"error": "Internal Server Error", "code": "INTERNAL_ERROR"},
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    async def search(request: Request) -> JSONResponse:
        if resolved_service is None:
            if service_build_error is not None:
                _LOGGER.warning("search request rejected because the gateway service is not configured")
            return JSONResponse(
                {"error": "Search gateway is not configured", "code": "SEARCH_GATEWAY_NOT_CONFIGURED"},
                status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            )

        payload = await _load_json_payload(request, runtime=runtime)
        if isinstance(payload, JSONResponse):
            return payload
        try:
            search_request = SearchGatewayRequest.model_validate(payload)
        except ValidationError as exc:
            return JSONResponse(
                {
                    "error": "Invalid search payload",
                    "code": "INVALID_SEARCH_REQUEST",
                    "details": exc.errors(include_url=False),
                },
                status_code=HTTPStatus.BAD_REQUEST,
            )

        try:
            response = await resolved_service.search(
                consumer_id=search_request.consumer_id,
                tenant_id=search_request.tenant_id,
                entity_type=search_request.entity_type,
                raw_params=search_request.params,
            )
            return JSONResponse(response)
        except ValueError as exc:
            return JSONResponse(
                {"error": str(exc), "code": "INVALID_SEARCH_REQUEST"},
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            )
        except Exception:
            _LOGGER.exception("search request failed")
            return JSONResponse(
                {"error": "Search backend failure", "code": "SEARCH_GATEWAY_ERROR"},
                status_code=HTTPStatus.BAD_GATEWAY,
            )

    return Starlette(
        debug=False,
        middleware=[Middleware(cast(Any, APIKeyAndBodyLimitMiddleware), config=runtime)],
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/translate", translate, methods=["POST"]),
            Route("/search", search, methods=["POST"]),
        ],
    )


async def _load_json_payload(
    request: Request,
    *,
    runtime: ASGIRuntimeConfig,
) -> dict[str, object] | JSONResponse:
    body = await request.body()
    if len(body) > runtime.max_body_bytes:
        return JSONResponse(
            {"error": "Request body too large", "code": "REQUEST_TOO_LARGE"},
            status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
        )
    try:
        payload = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse(
            {"error": "Invalid JSON payload", "code": "INVALID_JSON"},
            status_code=HTTPStatus.BAD_REQUEST,
        )
    if not isinstance(payload, dict):
        return JSONResponse(
            {"error": "JSON body must be an object", "code": "INVALID_PAYLOAD"},
            status_code=HTTPStatus.BAD_REQUEST,
        )
    return payload


app = build_http_gateway_app()
