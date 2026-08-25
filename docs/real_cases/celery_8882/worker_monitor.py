"""Monitor the native Celery process through a shared PID namespace."""

from __future__ import annotations

import json
import os
import re
import signal
import time
from datetime import datetime, timezone
from pathlib import Path


EVIDENCE = Path(os.environ.get("CELERY_EVIDENCE_DIR", "/evidence"))
OBSERVATIONS = EVIDENCE / "worker_observations.ndjson"
CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def worker_pid() -> int | None:
    for item in sorted(Path("/proc").iterdir(), key=lambda path: path.name):
        if not item.name.isdigit():
            continue
        try:
            command = (item / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if "celery" in command and "worker" in command:
            return int(item.name)
    return None


def container_id() -> str:
    try:
        text = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    matches = CONTAINER_ID_RE.findall(text)
    return matches[-1] if matches else ""


def rss_bytes(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def emit(event: str, pid: int | None, **fields: object) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    payload = {
        "observed_at": utcnow(),
        "event": event,
        "monitor_pid": os.getpid(),
        "worker_pid": pid,
        "container_id": container_id(),
        "rss_bytes": rss_bytes(pid) if pid else None,
        "source_root": os.environ.get("CELERY_SOURCE_ROOT", ""),
        "container_workdir": os.environ.get("CELERY_CONTAINER_WORKDIR", ""),
        "repo_revision": os.environ.get("CELERY_REPO_REVISION", ""),
        **fields,
    }
    with OBSERVATIONS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    print(json.dumps(payload, sort_keys=True), flush=True)


def main() -> int:
    interval = max(0.2, float(os.environ.get("CELERY_SAMPLE_INTERVAL_SEC", "1")))
    release_path = EVIDENCE / "runner-release.json"
    emit("monitor_start", None)
    pid = None
    while not release_path.exists():
        pid = worker_pid()
        if pid is None:
            emit("worker_not_found", None)
            time.sleep(interval)
            continue
        emit("rss_sample", pid)
        time.sleep(interval)
    pid = worker_pid() or pid
    if pid:
        os.kill(pid, signal.SIGTERM)
    emit("worker_exit_requested", pid, reason="runner_release")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
