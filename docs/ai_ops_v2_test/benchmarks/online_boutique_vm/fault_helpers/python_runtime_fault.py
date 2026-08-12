"""Bounded Python memory-growth and lock-contention fault helper."""

from __future__ import annotations

import argparse
import threading
import time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("memory", "lock", "compound"), required=True)
    parser.add_argument("--duration", type=int, default=180)
    parser.add_argument("--threads", type=int, default=12)
    parser.add_argument("--chunk-mb", type=int, default=4)
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--start-delay", type=float, default=0.0)
    args = parser.parse_args()

    lock = threading.Lock()
    lock.acquire()
    if args.mode in {"lock", "compound"}:
        for _ in range(max(2, min(args.threads, 64))):
            threading.Thread(target=lock.acquire, daemon=True).start()

    allocations: list[bytearray] = []
    time.sleep(max(0.0, min(args.start_delay, 30.0)))
    deadline = time.monotonic() + max(5, min(args.duration, 600))
    while time.monotonic() < deadline:
        if args.mode in {"memory", "compound"}:
            block = bytearray(max(1, min(args.chunk_mb, 32)) * 1024 * 1024)
            block[::4096] = b"x" * len(block[::4096])
            allocations.append(block)
        time.sleep(max(0.1, min(args.interval, 5.0)))


if __name__ == "__main__":
    main()
