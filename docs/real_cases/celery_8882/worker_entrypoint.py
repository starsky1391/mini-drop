"""Run a native Celery worker and record host-visible runtime metadata."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


EVIDENCE = Path(os.environ.get("CELERY_EVIDENCE_DIR", "/evidence"))
OBSERVATIONS = EVIDENCE / "worker_observations.ndjson"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(event: str, **fields: object) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    payload = {
        "observed_at": utcnow(),
        "event": event,
        "worker_pid": os.getpid(),
        "source_root": os.environ.get("CELERY_SOURCE_ROOT", ""),
        "container_workdir": os.environ.get("CELERY_CONTAINER_WORKDIR", ""),
        "repo_revision": os.environ.get("CELERY_REPO_REVISION", ""),
        **fields,
    }
    with OBSERVATIONS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    print(json.dumps(payload, sort_keys=True), flush=True)


def main() -> int:
    pool = os.environ.get("CELERY_WORKER_POOL", "solo")
    concurrency = os.environ.get("CELERY_WORKER_CONCURRENCY", "1")
    command = [
        "celery", "-A", "celery_case_tasks", "worker",
        "--loglevel=INFO", f"--pool={pool}", f"--concurrency={concurrency}",
        "--queues=failure-workload",
        "--hostname=failure-worker@%h", "--without-gossip", "--without-mingle",
    ]
    emit("worker_start", command=command)
    os.execvp(command[0], command)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
