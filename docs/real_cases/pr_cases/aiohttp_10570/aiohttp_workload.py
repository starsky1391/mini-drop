from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web


EVIDENCE = Path(os.environ.get("CASE_EVIDENCE_ROOT", "/evidence"))
PAYLOAD = b"x" * (8 * 1024 * 1024)


async def handler(request: web.Request) -> web.StreamResponse:
    response = web.StreamResponse(headers={"Content-Length": str(len(PAYLOAD))})
    await response.prepare(request)
    for offset in range(0, len(PAYLOAD), 4096):
        await response.write(PAYLOAD[offset:offset + 4096])
    await response.write_eof()
    return response


def emit(event: str, **fields: object) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with (EVIDENCE / "workload.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, "observed_at": time.time(), **fields}) + "\n")


async def main() -> None:
    app = web.Application()
    app.router.add_get("/large", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 18080)
    await site.start()
    (EVIDENCE / "ready").touch()
    deadline = time.monotonic() + max(30, int(os.environ.get("CASE_DURATION_SEC", "180")))
    async with ClientSession(timeout=ClientTimeout(total=30)) as session:
        while time.monotonic() < deadline:
            try:
                async with session.get(
                    "http://127.0.0.1:18080/large",
                    timeout=ClientTimeout(total=30),
                ) as response:
                    body = await response.read()
                    emit("response_read", bytes=len(body), status=response.status)
                    await asyncio.sleep(0.1)
            except Exception as exc:
                emit("response_read_error", error=type(exc).__name__, message=str(exc))
                await asyncio.sleep(1.0)
    await runner.cleanup()
    (EVIDENCE / "complete").touch()


asyncio.run(main())
