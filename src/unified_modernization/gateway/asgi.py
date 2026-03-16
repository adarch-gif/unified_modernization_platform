from __future__ import annotations

import json
import logging
import os
from http import HTTPStatus
from typing import Any, cast

from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from unified_modernization.gateway.odata import ODataTranslator


_NON_PRODUCTION_ENVIRONMENTS = {"local", "dev", "test"}
_LOGGER = logging.getLogger("unified_modernization.gateway.asgi")


class ASGIRuntimeConfig(BaseModel):
    environment: str = "dev"
    valid_api_keys: set[str] = Field(default_factory=set)
    max_body_bytes: int = Field(default=64 * 1024, gt=0)
    field_map: dict[str, str] = Field(default_factory=dict)


def _load_runtime_config() -> ASGIRuntimeConfig:
    return ASGIRuntimeConfig(
        environment=os.getenv("GATEWAY_ENVIRONMENT", "dev").lower(),
        valid_api_keys={item.strip() for item in os.getenv("GATEWAY_API_KEYS", "").split(",") if item.strip()},
        max_body_bytes=int(os.getenv("GATEWAY_MAX_BODY_BYTES", str(64 * 1024))),
        field_map={
            item.split("=", 1)[0].strip(): item.split("=", 1)[1].strip()
            for item in os.getenv("GATEWAY_FIELD_MAP", "").split(",")
            if "=" in item
        },
    )


class APIKeyAndBodyLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Starlette, *, config: ASGIRuntimeConfig) -> None:
        super().__init__(app)
        self._config = config

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path == "/health":
            return await call_next(request)

        if request.method in {"POST", "PUT", "PATCH"}:
            content_length = request.headers.get("content-length")
            if content_length is not None:
                try:
                    if int(content_length) > self._config.max_body_bytes:
                        return JSONResponse(
                            {"error": "Request body too large", "code": "REQUEST_TOO_LARGE"},
                            status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        )
                except ValueError:
                    return JSONResponse(
                        {"error": "Invalid Content-Length header", "code": "INVALID_CONTENT_LENGTH"},
                        status_code=HTTPStatus.BAD_REQUEST,
                    )

        if self._config.environment not in _NON_PRODUCTION_ENVIRONMENTS:
            key = request.headers.get("X-API-Key", "")
            if not self._config.valid_api_keys or key not in self._config.valid_api_keys:
                return JSONResponse(
                    {"error": "Unauthorized", "code": "UNAUTHORIZED"},
                    status_code=HTTPStatus.UNAUTHORIZED,
                )
        return await call_next(request)


def build_app(config: ASGIRuntimeConfig | None = None) -> Starlette:
    runtime = config or _load_runtime_config()
    translator = ODataTranslator(runtime.field_map)

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "service": "search-gateway-starter"})

    async def translate(request: Request) -> JSONResponse:
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

    return Starlette(
        debug=False,
        middleware=[Middleware(cast(Any, APIKeyAndBodyLimitMiddleware), config=runtime)],
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/translate", translate, methods=["POST"]),
        ],
    )


app = build_app()
