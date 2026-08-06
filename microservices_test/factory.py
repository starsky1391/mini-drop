from __future__ import annotations

import time
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from microservices_test.common import health_payload, json_log, make_context


def build_app(service: str, description: str) -> FastAPI:
    app = FastAPI(title=f"Mini-Drop Test {service}", description=description, version="0.1.0")

    @app.middleware("http")
    async def _trace_and_log(request: Request, call_next):
        context = make_context(
            service=service,
            scenario=request.headers.get("x-scenario", request.query_params.get("scenario", "default")),
            trace_id=request.headers.get("x-trace-id"),
            request_id=request.headers.get("x-request-id"),
        )
        request.state.trace_id = context.trace_id
        request.state.request_id = context.request_id
        request.state.scenario = context.scenario
        start = time.perf_counter()
        json_log(service, "request_start", trace_id=context.trace_id, request_id=context.request_id, path=request.url.path, scenario=context.scenario)
        try:
            response = await call_next(request)
        except Exception as exc:
            json_log(
                service,
                "request_error",
                trace_id=context.trace_id,
                request_id=context.request_id,
                path=request.url.path,
                error=type(exc).__name__,
            )
            raise
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        response.headers["x-trace-id"] = context.trace_id
        response.headers["x-request-id"] = context.request_id
        response.headers["x-scenario"] = context.scenario
        json_log(
            service,
            "request_end",
            trace_id=context.trace_id,
            request_id=context.request_id,
            path=request.url.path,
            status_code=response.status_code,
            latency_ms=latency_ms,
        )
        return response

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse(health_payload(service))

    @app.get("/")
    def root() -> dict[str, Any]:
        return {"service": service, "description": description}

    return app

