from __future__ import annotations

import os
from http import HTTPStatus

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from unified_modernization.gateway.odata import ODataTranslator


_NON_PRODUCTION_ENVIRONMENTS = {"local", "dev", "test"}
_ENVIRONMENT = os.getenv("GATEWAY_ENVIRONMENT", "dev").lower()
_VALID_API_KEYS = {item.strip() for item in os.getenv("GATEWAY_API_KEYS", "").split(",") if item.strip()}
_MAX_BODY_BYTES = int(os.getenv("GATEWAY_MAX_BODY_BYTES", str(64 * 1024)))
_FIELD_MAP = {
    item.split("=", 1)[0].strip(): item.split("=", 1)[1].strip()
    for item in os.getenv("GATEWAY_FIELD_MAP", "").split(",")
    if "=" in item
}

translator = ODataTranslator(_FIELD_MAP)


class APIKeyAndBodyLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path == "/health":
            return await call_next(request)

        if request.method in {"POST", "PUT", "PATCH"}:
            content_length = request.headers.get("content-length")
            if content_length is not None and int(content_length) > _MAX_BODY_BYTES:
                return JSONResponse({"error": "Request body too large"}, status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

        if _ENVIRONMENT not in _NON_PRODUCTION_ENVIRONMENTS:
            key = request.headers.get("X-API-Key", "")
            if not _VALID_API_KEYS or key not in _VALID_API_KEYS:
                return JSONResponse({"error": "Unauthorized"}, status_code=HTTPStatus.UNAUTHORIZED)
        return await call_next(request)


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "search-gateway-starter"})


async def translate(request: Request) -> JSONResponse:
    body = await request.body()
    if len(body) > _MAX_BODY_BYTES:
        return JSONResponse({"error": "Request body too large"}, status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
    try:
        payload = await request.json()
        params = payload.get("params", {})
        return JSONResponse({"query": translator.translate(params)})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=HTTPStatus.BAD_REQUEST)
    except Exception:
        return JSONResponse({"error": "Internal Server Error"}, status_code=HTTPStatus.INTERNAL_SERVER_ERROR)


app = Starlette(
    debug=False,
    middleware=[Middleware(APIKeyAndBodyLimitMiddleware)],
    routes=[
        Route("/health", health, methods=["GET"]),
        Route("/translate", translate, methods=["POST"]),
    ],
)
