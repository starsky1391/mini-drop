from __future__ import annotations

import json
import os
import time
from pathlib import Path


EVIDENCE = Path(os.environ.get("CASE_EVIDENCE_ROOT", "/evidence"))
PATTERN = os.environ.get("TARGET_PROCESS_PATTERN", "")
def find_target() -> int | None:
    for path in sorted(Path("/proc").iterdir(), key=lambda item: item.name):
        if not path.name.isdigit():
            continue
        try:
            cmd = (path / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if PATTERN and PATTERN in cmd:
            return int(path.name)
    return None


def rss(pid: int | None) -> int | None:
    if not pid:
        return None
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def emit(event: str, **fields: object) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    payload = {"observed_at": time.time(), "event": event, **fields}
    with (EVIDENCE / "worker_observations.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


while not (EVIDENCE / "runner-release.json").exists():
    pid = find_target()
    emit("rss_sample", pid=pid, rss_bytes=rss(pid))
    time.sleep(1)
emit("monitor_complete")
