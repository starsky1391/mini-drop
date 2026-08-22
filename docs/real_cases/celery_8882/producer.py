"""Submit real Celery tasks and write bounded producer observations."""

from __future__ import annotations

import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

from celery_case_tasks import app, batch_barrier, control_success, unhandled_failure, warmup_barrier


EVIDENCE = Path(os.environ.get("CELERY_EVIDENCE_DIR", "/evidence"))
OBSERVATIONS = EVIDENCE / "producer_observations.ndjson"
TASK_OBSERVATIONS = EVIDENCE / "task_observations.ndjson"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(event: str, **fields: object) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    payload = {
        "observed_at": utcnow(),
        "event": event,
        "producer_pid": os.getpid(),
        "producer_container": socket.gethostname(),
        "source_root": os.environ.get("CELERY_SOURCE_ROOT", ""),
        "container_workdir": os.environ.get("CELERY_CONTAINER_WORKDIR", ""),
        "repo_revision": os.environ.get("CELERY_REPO_REVISION", ""),
        **fields,
    }
    with OBSERVATIONS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    print(json.dumps(payload, sort_keys=True), flush=True)


def wait_for_worker_event(event: str, *, batch: int | None = None, timeout: float = 300.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if TASK_OBSERVATIONS.exists():
            for line in TASK_OBSERVATIONS.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("event") == event and (batch is None or row.get("batch") == batch):
                    return row
        time.sleep(0.25)
    raise TimeoutError(f"worker did not emit {event} for batch {batch}")


def _run() -> None:
    warmup_count = max(0, int(os.environ.get("CELERY_WARMUP_COUNT", "1000")))
    failure_count = max(1, int(os.environ.get("CELERY_FAILURE_COUNT", "1000")))
    failure_batches = max(1, int(os.environ.get("CELERY_FAILURE_BATCHES", "2")))
    interval = max(0.0, float(os.environ.get("CELERY_PRODUCER_INTERVAL_SEC", "0")))
    settle_seconds = max(0.0, float(os.environ.get("CELERY_WARMUP_SETTLE_SEC", "10")))
    emit(
        "producer_start",
        broker="redis://redis:6379/0",
        warmup_count=warmup_count,
        failure_count=failure_count,
        failure_batches=failure_batches,
        warmup_settle_sec=settle_seconds,
    )
    app.connection_for_write().ensure_connection(max_retries=60, interval_start=0.2, interval_step=0.5, interval_max=2)
    submitted_controls = 0
    for submitted_controls in range(1, warmup_count + 1):
        control_success.apply_async(args=[submitted_controls], queue="failure-workload")
        if interval:
            time.sleep(interval)
    emit("warmup_complete", submitted_controls=submitted_controls)
    warmup_barrier.apply_async(args=[submitted_controls], queue="failure-workload")
    wait_for_worker_event("warmup_barrier")
    time.sleep(settle_seconds)
    submitted_failures = 0
    for batch in range(1, failure_batches + 1):
        for offset in range(1, failure_count + 1):
            submitted_failures += 1
            unhandled_failure.apply_async(args=[submitted_failures], queue="failure-workload")
            if offset == 1 or offset % 100 == 0:
                emit(
                    "submission_sample",
                    batch=batch,
                    batch_submitted_failures=offset,
                    submitted_failures=submitted_failures,
                    submitted_controls=submitted_controls,
                )
            if interval:
                time.sleep(interval)
        batch_barrier.apply_async(args=[batch, submitted_failures], queue="failure-workload")
        barrier = wait_for_worker_event("failure_batch_barrier", batch=batch)
        emit(
            "failure_batch_complete",
            batch=batch,
            submitted_failures=submitted_failures,
            worker_barrier_observed_at=barrier["observed_at"],
            worker_barrier_rss_bytes=barrier.get("rss_bytes"),
            worker_barrier_tracemalloc_current_bytes=barrier.get("tracemalloc_current_bytes"),
            worker_barrier_gc_collected=barrier.get("gc_collected"),
        )
        if batch < failure_batches:
            time.sleep(settle_seconds)
    emit(
        "workload_complete",
        submitted_failures=submitted_failures,
        submitted_controls=submitted_controls,
        completed_batches=failure_batches,
    )
    emit(
        "producer_complete",
        submitted_failures=submitted_failures,
        submitted_controls=submitted_controls,
    )


def main() -> None:
    try:
        _run()
    except Exception as exc:
        emit(
            "producer_failed",
            error_type=type(exc).__name__,
            error=str(exc)[:500],
        )
        raise


if __name__ == "__main__":
    main()
