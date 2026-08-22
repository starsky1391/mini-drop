#!/usr/bin/env python3
"""Attach Memray to a container process from a host-pid Agent namespace."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path


def _container_pid(host_pid: int) -> int:
    status_path = Path(f"/proc/{host_pid}/status")
    for line in status_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("NSpid:"):
            values = [int(value) for value in line.split()[1:]]
            if values:
                return values[-1]
    return host_pid


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True, help="host-visible target PID")
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
        print("memray_namespace_helper_requires_root", flush=True)
        return 3

    nsenter = shutil.which("nsenter")
    memray = shutil.which("memray")
    if not nsenter:
        print("nsenter_not_installed", flush=True)
        return 4
    if not memray:
        print("memray_not_installed", flush=True)
        return 5

    try:
        target_pid = _container_pid(args.pid)
    except (OSError, ValueError):
        print("target_pid_namespace_unreadable", flush=True)
        return 6

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        nsenter,
        "-t",
        str(args.pid),
        "-p",
        "--",
        memray,
        "attach",
        "--output",
        str(output),
        "--duration",
        str(args.duration),
        str(target_pid),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=args.duration + 45,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(str(exc), flush=True)
        return 7

    if result.returncode != 0:
        if result.stderr:
            print(result.stderr.decode("utf-8", errors="replace")[-1000:], flush=True)
        return result.returncode or 8
    if not output.is_file() or output.stat().st_size <= 0:
        print("memray_attach_output_missing", flush=True)
        return 9
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
