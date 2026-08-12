"""Bounded HTTP client for an isolated network-namespace fault test."""

from __future__ import annotations

import argparse
import os
import time
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--duration", type=int, default=150)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--interval", type=float, default=0.1)
    args = parser.parse_args()
    log = open(f"/tmp/mini-drop-network-fault-{os.getpid()}.log", "w", buffering=1)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + max(5, min(args.duration, 600))
    while time.monotonic() < deadline:
        started = time.monotonic()
        try:
            response = opener.open(args.url, timeout=max(0.2, min(args.timeout, 10.0)))
            response.read(1024)
            response.close()
            log.write(f"request ok latency_ms={(time.monotonic() - started) * 1000:.1f}\n")
        except Exception as exc:
            log.write(f"request timeout or connection failed: {type(exc).__name__}: {exc}\n")
        time.sleep(max(0.02, min(args.interval, 5.0)))


if __name__ == "__main__":
    main()
