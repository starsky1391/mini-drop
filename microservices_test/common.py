from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

import requests


def new_trace_id() -> str:
    return uuid.uuid4().hex


def stable_request_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    request_id: str
    scenario: str
    service: str


def make_context(service: str, scenario: str, trace_id: str | None = None, request_id: str | None = None) -> TraceContext:
    return TraceContext(
        trace_id=trace_id or new_trace_id(),
        request_id=request_id or stable_request_id(),
        scenario=scenario or "default",
        service=service,
    )


def json_log(service: str, stage: str, **fields: Any) -> None:
    payload = {
        "ts": round(time.time(), 3),
        "service": service,
        "stage": stage,
        **fields,
    }
    print(json.dumps(payload, ensure_ascii=False, default=str), flush=True)


def request_headers(context: TraceContext, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "x-trace-id": context.trace_id,
        "x-request-id": context.request_id,
        "x-scenario": context.scenario,
        "x-origin-service": context.service,
    }
    if extra:
        headers.update(extra)
    return headers


def downstream_get(
    service: str,
    url: str,
    path: str,
    context: TraceContext,
    params: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    target = f"{url.rstrip('/')}{path}"
    start = time.perf_counter()
    response = requests.get(target, params=params, headers=request_headers(context), timeout=timeout)
    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    json_log(
        service,
        "downstream_ok",
        trace_id=context.trace_id,
        request_id=context.request_id,
        scenario=context.scenario,
        target=target,
        status_code=response.status_code,
        latency_ms=latency_ms,
    )
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict):
        return data
    return {"data": data}


def busy_cpu(rounds: int = 8000) -> dict[str, Any]:
    checksum = 0
    last_value = 0
    for i in range(1, rounds + 1):
        last_value = (i * i * 31 + checksum) % 10000019
        checksum = (checksum + last_value + (i % 97)) % 10000019
    return {"checksum": checksum, "last_value": last_value, "rounds": rounds}


def line_hotspot(rounds: int = 3500) -> dict[str, Any]:
    total = 0
    last_branch = 0
    for i in range(rounds):
        # 这里故意保持多行、稳定、纯 Python 的热点，便于重复任务下行为一致。
        branch = (i * 7 + total) % 11
        if branch in {0, 1, 2}:
            total += i * 3
        elif branch in {3, 4, 5}:
            total += i * 5
        else:
            total += i * 7
        last_branch = branch
    return {"total": total, "last_branch": last_branch, "rounds": rounds}


def blocking_wait(ms: int = 50) -> dict[str, Any]:
    lock = threading.Lock()
    with lock:
        time.sleep(ms / 1000.0)
    return {"slept_ms": ms}


def stable_digest(payload: dict[str, Any]) -> str:
    material = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


def health_payload(service: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "service": service,
        "status": "ok",
        "pid": os.getpid(),
    }
    if extra:
        payload.update(extra)
    return payload

