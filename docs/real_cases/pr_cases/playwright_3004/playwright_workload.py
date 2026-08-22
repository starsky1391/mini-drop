from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from playwright.async_api import async_playwright


EVIDENCE = Path(os.environ.get("CASE_EVIDENCE_ROOT", "/evidence"))


def emit(event: str, **fields: object) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with (EVIDENCE / "workload.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, "observed_at": time.time(), **fields}) + "\n")


async def main() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / "ready").touch()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            executable_path="/usr/bin/chromium",
            args=["--no-sandbox"],
        )
        emit("started")
        deadline = time.monotonic() + max(30, int(os.environ.get("CASE_DURATION_SEC", "180")))
        while time.monotonic() < deadline:
            async def operation() -> None:
                page = await browser.new_page()
                await page.goto("data:text/html,<title>mini-drop</title>")
                await page.close()

            tasks = [asyncio.create_task(operation()) for _ in range(4)]
            await asyncio.sleep(0.01)
            await asyncio.gather(*tasks, return_exceptions=True)
            emit("concurrent_operations")
        await browser.close()
    (EVIDENCE / "complete").touch()


asyncio.run(main())
