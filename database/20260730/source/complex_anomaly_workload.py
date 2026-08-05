#!/usr/bin/env python3
"""Bounded mixed anomaly workload for Mini-Drop data collection.

The process models a service with three concurrent symptoms:

1. CPU-heavy request serialization, sorting, and SHA-256 calculation.
2. A slowly growing in-memory cache, capped at 96 MiB.
3. Synchronous audit-log writes with fsync and bounded file rotation.

The workload automatically stops after ``--duration`` seconds and removes its
temporary I/O file. It uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import threading
import time
from pathlib import Path


def cpu_hotspot(stop: threading.Event, counters: dict[str, int]) -> None:
    rng = random.Random(20260730)
    while not stop.is_set():
        rows = [
            {
                "request_id": index,
                "score": rng.random(),
                "tags": ["mini-drop", "mixed-anomaly", str(index % 17)],
            }
            for index in range(6000)
        ]
        rows.sort(key=lambda item: item["score"])
        payload = json.dumps(rows, separators=(",", ":")).encode("utf-8")
        hashlib.sha256(payload).hexdigest()
        counters["cpu_batches"] += 1


def cache_growth(
    stop: threading.Event,
    counters: dict[str, int],
    *,
    max_cache_mib: int,
) -> None:
    cache: list[bytearray] = []
    while not stop.wait(1.0):
        if len(cache) < max_cache_mib:
            block = bytearray(1024 * 1024)
            for offset in range(0, len(block), 4096):
                block[offset] = (len(cache) + offset) % 251
            cache.append(block)
            counters["cache_mib"] = len(cache)


def synchronous_audit_io(
    stop: threading.Event,
    counters: dict[str, int],
    *,
    io_path: Path,
    rotate_mib: int,
) -> None:
    block = os.urandom(256 * 1024)
    rotate_bytes = rotate_mib * 1024 * 1024
    try:
        while not stop.is_set():
            mode = "r+b" if io_path.exists() else "w+b"
            with io_path.open(mode, buffering=0) as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() >= rotate_bytes:
                    handle.seek(0)
                    handle.truncate()
                    counters["rotations"] += 1
                handle.write(block)
                handle.flush()
                os.fsync(handle.fileno())
                counters["io_bytes"] += len(block)
            stop.wait(0.15)
    finally:
        io_path.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=int, default=240)
    parser.add_argument("--max-cache-mib", type=int, default=96)
    parser.add_argument("--rotate-mib", type=int, default=64)
    parser.add_argument(
        "--io-path",
        type=Path,
        default=Path("/tmp/mini_drop_complex_anomaly.bin"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    stop = threading.Event()
    counters = {
        "cpu_batches": 0,
        "cache_mib": 0,
        "io_bytes": 0,
        "rotations": 0,
    }
    started_at = time.time()
    threads = [
        threading.Thread(
            name="cpu-hotspot",
            target=cpu_hotspot,
            args=(stop, counters),
        ),
        threading.Thread(
            name="cache-growth",
            target=cache_growth,
            args=(stop, counters),
            kwargs={"max_cache_mib": args.max_cache_mib},
        ),
        threading.Thread(
            name="sync-audit-io",
            target=synchronous_audit_io,
            args=(stop, counters),
            kwargs={"io_path": args.io_path, "rotate_mib": args.rotate_mib},
        ),
    ]
    for thread in threads:
        thread.start()

    print(json.dumps({
        "event": "workload_started",
        "pid": os.getpid(),
        "duration_sec": args.duration,
        "max_cache_mib": args.max_cache_mib,
        "rotate_mib": args.rotate_mib,
        "io_path": str(args.io_path),
        "expected_symptoms": [
            "single-core CPU saturation",
            "increasing process RSS",
            "synchronous write and fsync activity",
        ],
    }, ensure_ascii=False), flush=True)

    try:
        deadline = time.monotonic() + args.duration
        while time.monotonic() < deadline:
            time.sleep(min(5.0, max(0.0, deadline - time.monotonic())))
            print(json.dumps({
                "event": "workload_progress",
                "elapsed_sec": round(time.time() - started_at, 1),
                **counters,
            }, ensure_ascii=False), flush=True)
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=10)
        print(json.dumps({
            "event": "workload_finished",
            "elapsed_sec": round(time.time() - started_at, 1),
            **counters,
        }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
