from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from microservices_test.common import TraceContext, busy_cpu, downstream_get, stable_digest
from microservices_test.factory import build_app


ORDER_URL = "http://order:8080"
INVENTORY_URL = "http://inventory:8080"

app = build_app("payment", "支付服务，提供冲突场景和混合证据。")


def _context(request: Request, scenario: str) -> TraceContext:
    return TraceContext(
        trace_id=request.state.trace_id,
        request_id=request.state.request_id,
        scenario=scenario,
        service="payment",
    )


@app.get("/charge")
def charge(request: Request, rounds: int = 7000) -> JSONResponse:
    scenario = request.state.scenario or "charge"
    ctx = _context(request, scenario)
    cpu = busy_cpu(rounds)
    digest = stable_digest({"cpu": cpu, "scenario": scenario})
    return JSONResponse(
        {
            "service": "payment",
            "scenario": scenario,
            "trace_id": ctx.trace_id,
            "request_id": ctx.request_id,
            "charge": cpu,
            "digest": digest,
        }
    )


@app.get("/conflict")
def conflict(request: Request, rounds: int = 8000, wait_ms: int = 100) -> JSONResponse:
    scenario = request.state.scenario or "conflict"
    ctx = _context(request, scenario)
    cpu = busy_cpu(rounds)
    order = downstream_get("payment", ORDER_URL, "/cpu_hotspot", ctx, params={"rounds": max(4000, rounds // 2)})
    inventory = downstream_get("payment", INVENTORY_URL, "/offcpu_wait", ctx, params={"ms": wait_ms})
    return JSONResponse(
        {
            "service": "payment",
            "scenario": scenario,
            "trace_id": ctx.trace_id,
            "request_id": ctx.request_id,
            "conflict": {
                "cpu": cpu,
                "order": order,
                "inventory": inventory,
                "digest": stable_digest({"cpu": cpu, "order": order, "inventory": inventory}),
            },
        }
    )
