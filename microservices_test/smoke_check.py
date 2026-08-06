from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import requests


def _wait_ready(base_url: str, path: str, timeout_sec: int = 90) -> dict[str, Any]:
    deadline = time.time() + timeout_sec
    last_error = ""
    while time.time() < deadline:
        try:
            resp = requests.get(f"{base_url.rstrip('/')}{path}", timeout=5)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(1)
    raise RuntimeError(last_error or "timeout")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18084")
    parser.add_argument("--repeat", type=int, default=2)
    args = parser.parse_args()

    scenarios = ["cpu_hotspot", "offcpu_wait", "chain", "conflict", "line_hotspot", "repeat"]
    results: list[dict[str, Any]] = []

    status = _wait_ready(args.base_url, "/status")
    print(json.dumps({"loadgen_status": status}, ensure_ascii=False, indent=2))

    for scenario in scenarios:
        resp = requests.get(f"{args.base_url.rstrip('/')}/run/{scenario}", params={"repeat": args.repeat}, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        results.append({"scenario": scenario, "count": len(data.get("results", [])), "ok": True})
        print(f"[ok] {scenario}: {len(data.get('results', []))} runs")

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

