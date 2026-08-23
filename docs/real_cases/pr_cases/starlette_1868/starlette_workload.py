from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.routing import Route


EVIDENCE = Path(os.environ.get("CASE_EVIDENCE_ROOT", "/evidence"))


class PassThroughMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        return await call_next(request)


async def stream_endpoint(request: Request) -> StreamingResponse:
    async def body():
        for _ in range(128):
            yield b"x" * 4096
            await asyncio.sleep(0)

    return StreamingResponse(body(), media_type="application/octet-stream")


app = Starlette(routes=[Route("/stream", stream_endpoint)])
app.add_middleware(PassThroughMiddleware)


def emit(event: str, **fields: object) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with (EVIDENCE / "workload.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, "observed_at": time.time(), **fields}, sort_keys=True) + "\n")


def serve() -> None:
    config = uvicorn.Config(app, host="127.0.0.1", port=18080, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    asyncio.run(server.serve())


async def run_client() -> None:
    deadline = time.monotonic() + max(30, int(os.environ.get("CASE_DURATION_SEC", "180")))
    count = 0
    async with httpx.AsyncClient(timeout=10) as client:
        while time.monotonic() < deadline:
            started = time.perf_counter()
            response = await client.get("http://127.0.0.1:18080/stream")
            elapsed_ms = (time.perf_counter() - started) * 1000
            count += 1
            if count % 10 == 0:
                emit("endpoint_batch", count=count, latency_ms=elapsed_ms, bytes=len(response.content), status=response.status_code)
    emit("workload_complete", count=count)
    (EVIDENCE / "complete").touch()


async def main() -> None:
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            async with httpx.AsyncClient(timeout=1) as client:
                await client.get("http://127.0.0.1:18080/stream")
            break
        except Exception:
            await asyncio.sleep(0.1)
    else:
        raise RuntimeError("Starlette server did not become ready")
    (EVIDENCE / "ready").touch()
    await run_client()


if __name__ == "__main__":
    asyncio.run(main())
