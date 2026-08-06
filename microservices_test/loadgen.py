from __future__ import annotations

import os
import threading
import time
from typing import Any

import requests
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from microservices_test.common import json_log, new_trace_id, stable_request_id
from microservices_test.factory import build_app


GATEWAY_URL = os.getenv("GATEWAY_URL", "http://gateway:8080")

app = build_app("loadgen", "自动压测器，循环触发不同场景以覆盖 AI 树和链路回连。")

_lock = threading.Lock()
_worker: threading.Thread | None = None
_stop_event = threading.Event()
_status: dict[str, Any] = {"running": False, "plan": [], "last_error": "", "last_run_at": None}


def _plan() -> list[str]:
    raw = os.getenv("LOADGEN_PLAN", "cpu_hotspot,offcpu_wait,chain,conflict,line_hotspot,repeat")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _run_loop() -> None:
    interval = max(0.1, float(os.getenv("LOADGEN_REQUEST_GAP_SEC", "0.4")))
    cycle_delay = max(0.0, float(os.getenv("LOADGEN_CYCLE_DELAY_SEC", "1.0")))
    timeout = max(2.0, float(os.getenv("LOADGEN_REQUEST_TIMEOUT_SEC", "20")))
    plan = _plan()
    with _lock:
        _status.update({"running": True, "plan": plan, "last_error": "", "last_run_at": time.time()})
    while not _stop_event.is_set():
        for scenario in plan:
            if _stop_event.is_set():
                break
            trace_id = new_trace_id()
            request_id = stable_request_id()
            url = f"{GATEWAY_URL.rstrip('/')}/scenario/{scenario}"
            try:
                json_log("loadgen", "scenario_start", scenario=scenario, trace_id=trace_id, request_id=request_id, url=url)
                resp = requests.get(
                    url,
                    headers={
                        "x-trace-id": trace_id,
                        "x-request-id": request_id,
                        "x-scenario": scenario,
                    },
                    timeout=timeout,
                )
                resp.raise_for_status()
                json_log("loadgen", "scenario_done", scenario=scenario, trace_id=trace_id, request_id=request_id, status_code=resp.status_code)
                with _lock:
                    _status["last_run_at"] = time.time()
            except Exception as exc:
                with _lock:
                    _status["last_error"] = f"{scenario}: {type(exc).__name__}: {exc}"
                json_log("loadgen", "scenario_error", scenario=scenario, trace_id=trace_id, request_id=request_id, error=type(exc).__name__)
            time.sleep(interval)
        if cycle_delay > 0:
            time.sleep(cycle_delay)
    with _lock:
        _status["running"] = False


def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _stop_event.clear()
        _worker = threading.Thread(target=_run_loop, daemon=True)
        _worker.start()


@app.on_event("startup")
def _startup() -> None:
    if os.getenv("LOADGEN_AUTO_START", "1").strip().lower() in {"1", "true", "yes", "on"}:
        _ensure_worker()


@app.get("/status")
def status() -> JSONResponse:
    with _lock:
        payload = dict(_status)
        payload["gateway_url"] = GATEWAY_URL
    return JSONResponse(payload)


@app.post("/start")
def start() -> JSONResponse:
    _ensure_worker()
    return JSONResponse({"started": True, "plan": _plan()})


@app.post("/stop")
def stop() -> JSONResponse:
    _stop_event.set()
    return JSONResponse({"stopping": True})


@app.get("/run/{scenario}")
def run_once(scenario: str, repeat: int = 1, gap_ms: int = 250) -> JSONResponse:
    repeat = max(1, min(repeat, 100))
    gap_ms = max(0, min(gap_ms, 5000))
    results: list[dict[str, Any]] = []
    for _ in range(repeat):
        trace_id = new_trace_id()
        request_id = stable_request_id()
        url = f"{GATEWAY_URL.rstrip('/')}/scenario/{scenario}"
        resp = requests.get(
            url,
            headers={
                "x-trace-id": trace_id,
                "x-request-id": request_id,
                "x-scenario": scenario,
            },
            timeout=max(2.0, float(os.getenv("LOADGEN_REQUEST_TIMEOUT_SEC", "20"))),
        )
        results.append({"status_code": resp.status_code, "body": resp.json()})
        time.sleep(gap_ms / 1000.0)
    return JSONResponse({"scenario": scenario, "repeat": repeat, "results": results})

