#!/usr/bin/env python3
"""Bounded live native-allocation observation for an already running PID."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path


def _libc_path() -> str:
    candidates = [
        "/lib/x86_64-linux-gnu/libc.so.6",
        "/lib/aarch64-linux-gnu/libc.so.6",
        "/usr/lib/x86_64-linux-gnu/libc.so.6",
        "/usr/lib/aarch64-linux-gnu/libc.so.6",
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    raise RuntimeError("libc.so.6 was not found")


def _program(libc: str, duration: int) -> str:
    return f"""
uprobe:{libc}:malloc {{ @malloc_calls = count(); @malloc_bytes = sum(arg0); }}
uprobe:{libc}:calloc {{ @calloc_calls = count(); @calloc_bytes = sum(arg0 * arg1); }}
uprobe:{libc}:realloc {{ @realloc_calls = count(); @realloc_bytes = sum(arg1); }}
interval:s:1 {{
  @elapsed = count();
  if (@elapsed >= {duration}) {{ exit(); }}
}}
END {{
  printf("native_heap_live duration_sec={duration}\\n");
  print(@malloc_calls);
  print(@malloc_bytes);
  print(@calloc_calls);
  print(@calloc_bytes);
  print(@realloc_calls);
  print(@realloc_bytes);
}}
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--duration", type=int, required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    if args.pid <= 0 or not Path(f"/proc/{args.pid}").is_dir():
        print("target_process_missing", flush=True)
        return 2
    if args.duration <= 0:
        print("duration_must_be_positive", flush=True)
        return 2
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print("native_heap_live_requires_root", flush=True)
        return 3
    bpftrace = shutil.which("bpftrace")
    if not bpftrace:
        print("bpftrace_not_installed", flush=True)
        return 4

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    try:
        libc = _libc_path()
        handle = output.open("wb")
        result = subprocess.run(
            [
                bpftrace,
                "-p",
                str(args.pid),
                "-e",
                _program(libc, args.duration),
            ],
            stdout=handle,
            stderr=subprocess.PIPE,
            timeout=args.duration + 30,
            check=False,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(str(exc), flush=True)
        return 5
    finally:
        if handle is not None:
            handle.close()

    if result.returncode != 0:
        if result.stderr:
            print(result.stderr.decode("utf-8", errors="replace")[-1000:], flush=True)
        return result.returncode or 6
    if not output.is_file() or output.stat().st_size == 0:
        print("native_heap_live_output_missing", flush=True)
        return 7
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
