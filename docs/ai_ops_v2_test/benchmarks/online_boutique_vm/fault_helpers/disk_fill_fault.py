"""Fill only the supplied benchmark filesystem and remain observable."""

from __future__ import annotations

import argparse
import os
import time


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True)
    parser.add_argument("--duration", type=int, default=180)
    args = parser.parse_args()
    target = os.path.abspath(args.path)
    output = open(os.path.join(target, "fill.bin"), "wb", buffering=0)
    log = open(f"/tmp/mini-drop-disk-fault-{os.getpid()}.log", "w", buffering=1)
    block = b"x" * (1024 * 1024)
    try:
        while True:
            output.write(block)
    except OSError as exc:
        log.write(f"disk write failed: {exc}\n")
    time.sleep(max(5, min(args.duration, 600)))


if __name__ == "__main__":
    main()
