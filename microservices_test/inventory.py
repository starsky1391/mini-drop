from __future__ import annotations

import threading
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from microservices_test.common import TraceContext, blocking_wait, stable_digest
from microservices_test.factory import build_app


LOCK = threading.Lock()
app = build_app("inventory", "库存服务，提供 off-CPU / 阻塞 / 锁竞争场景。")


def _context(request: Request, scenario: str) -> TraceContext:
    return TraceContext(
        trace_id=request.state.trace_id,
        request_id=request.state.request_id,
        scenario=scenario,
        service="inventory",
    )


@app.get("/offcpu_wait")
def offcpu_wait(request: Request, ms: int = 120) -> JSONResponse:
    scenario = request.state.scenario or "offcpu_wait"
    ctx = _context(request, scenario)
    with LOCK:
        wait = blocking_wait(ms)
    return JSONResponse(
        {
            "service": "inventory",
            "scenario": scenario,
            "trace_id": ctx.trace_id,
            "request_id": ctx.request_id,
            "wait": wait,
            "digest": stable_digest(wait),
        }
    )


@app.get("/contention")
def contention(request: Request, ms: int = 80, loops: int = 4) -> JSONResponse:
    scenario = request.state.scenario or "contention"
    ctx = _context(request, scenario)
    held = []
    for _ in range(max(1, loops)):
        with LOCK:
            held.append(blocking_wait(ms))
    return JSONResponse(
        {
            "service": "inventory",
            "scenario": scenario,
            "trace_id": ctx.trace_id,
            "request_id": ctx.request_id,
            "contention": held,
        }
    )
