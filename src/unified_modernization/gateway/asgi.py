from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from unified_modernization.gateway.odata import ODataTranslator


translator = ODataTranslator()


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "search-gateway-starter"})


async def translate(request: Request) -> JSONResponse:
    payload = await request.json()
    params = payload.get("params", {})
    return JSONResponse({"query": translator.translate(params)})


app = Starlette(
    debug=False,
    routes=[
        Route("/health", health, methods=["GET"]),
        Route("/translate", translate, methods=["POST"]),
    ],
)
