from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from microservices_test.common import TraceContext, busy_cpu, downstream_get, line_hotspot, stable_digest
from microservices_test.factory import build_app


INVENTORY_URL = "http://inventory:8080"
PAYMENT_URL = "http://payment:8080"

app = build_app("order", "订单服务，提供 CPU 热点、链路串联和稳定重复路径。")


def _context(request: Request, scenario: str) -> TraceContext:
    return TraceContext(
        trace_id=request.state.trace_id,
        request_id=request.state.request_id,
        scenario=scenario,
        service="order",
    )


@app.get("/cpu_hotspot")
def cpu_hotspot(request: Request, rounds: int = 8000) -> JSONResponse:
    scenario = request.state.scenario or "cpu_hotspot"
    ctx = _context(request, scenario)
    cpu = busy_cpu(rounds)
    marker = stable_digest({"scenario": scenario, "rounds": rounds, "checksum": cpu["checksum"]})
    return JSONResponse(
        {
            "service": "order",
            "scenario": scenario,
            "trace_id": ctx.trace_id,
            "request_id": ctx.request_id,
            "cpu": cpu,
            "marker": marker,
        }
    )


@app.get("/line_hotspot")
def line_hotspot_route(request: Request, rounds: int = 6000) -> JSONResponse:
    scenario = request.state.scenario or "line_hotspot"
    ctx = _context(request, scenario)
    hotspot = line_hotspot(rounds)
    return JSONResponse(
        {
            "service": "order",
            "scenario": scenario,
            "trace_id": ctx.trace_id,
            "request_id": ctx.request_id,
            "line_hotspot": hotspot,
        }
    )


@app.get("/repeat")
def repeatable_path(request: Request, rounds: int = 7000) -> JSONResponse:
    scenario = request.state.scenario or "repeat"
    ctx = _context(request, scenario)
    first = busy_cpu(rounds)
    second = line_hotspot(2000)
    return JSONResponse(
        {
            "service": "order",
            "scenario": scenario,
            "trace_id": ctx.trace_id,
            "request_id": ctx.request_id,
            "result": {
                "cpu": first,
                "line": second,
                "digest": stable_digest({"cpu": first, "line": second}),
            },
        }
    )


@app.get("/chain")
def chain(request: Request, rounds: int = 8000, wait_ms: int = 80) -> JSONResponse:
    scenario = request.state.scenario or "chain"
    ctx = _context(request, scenario)
    cpu = busy_cpu(rounds)
    inventory = downstream_get("order", INVENTORY_URL, "/offcpu_wait", ctx, params={"ms": wait_ms})
    payment = downstream_get("order", PAYMENT_URL, "/charge", ctx, params={"rounds": max(2000, rounds // 2)})
    return JSONResponse(
        {
            "service": "order",
            "scenario": scenario,
            "trace_id": ctx.trace_id,
            "request_id": ctx.request_id,
            "chain": {
                "cpu": cpu,
                "inventory": inventory,
                "payment": payment,
            },
        }
    )
