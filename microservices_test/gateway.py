from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from microservices_test.common import TraceContext, downstream_get, health_payload, json_log
from microservices_test.factory import build_app


ORDER_URL = "http://order:8080"
INVENTORY_URL = "http://inventory:8080"
PAYMENT_URL = "http://payment:8080"

app = build_app("gateway", "统一入口，负责把请求分发到各个业务服务。")


def _context(request: Request, scenario: str) -> TraceContext:
    return TraceContext(
        trace_id=request.state.trace_id,
        request_id=request.state.request_id,
        scenario=scenario,
        service="gateway",
    )


@app.get("/scenario/{scenario}")
def run_scenario(scenario: str, request: Request) -> JSONResponse:
    ctx = _context(request, scenario)
    json_log("gateway", "scenario_dispatch", trace_id=ctx.trace_id, request_id=ctx.request_id, scenario=scenario)

    if scenario == "cpu_hotspot":
        payload = downstream_get("gateway", ORDER_URL, "/cpu_hotspot", ctx, params={"rounds": 16000})
    elif scenario == "offcpu_wait":
        payload = downstream_get("gateway", INVENTORY_URL, "/offcpu_wait", ctx, params={"ms": 120})
    elif scenario == "chain":
        payload = downstream_get("gateway", ORDER_URL, "/chain", ctx, params={"rounds": 8000, "wait_ms": 80})
    elif scenario == "conflict":
        payload = downstream_get("gateway", PAYMENT_URL, "/conflict", ctx, params={"rounds": 8000, "wait_ms": 100})
    elif scenario == "line_hotspot":
        payload = downstream_get("gateway", ORDER_URL, "/line_hotspot", ctx, params={"rounds": 12000})
    elif scenario == "repeat":
        payload = downstream_get("gateway", ORDER_URL, "/repeat", ctx, params={"rounds": 9000})
    else:
        payload = {"scenario": scenario, "detail": "unknown scenario"}

    return JSONResponse(
        {
            "service": "gateway",
            "scenario": scenario,
            "trace_id": ctx.trace_id,
            "request_id": ctx.request_id,
            "result": payload,
        }
    )

