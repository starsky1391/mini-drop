"""Native Celery tasks for an unhandled-failure memory regression workload."""

from __future__ import annotations

import os
import gc
import json
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import tracemalloc
except ImportError:  # pragma: no cover - standard library on supported runtimes
    tracemalloc = None

from celery import Celery


app = Celery("failure_workload", broker=os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/0"))
app.conf.update(
    task_ignore_result=True,
    task_store_errors_even_if_ignored=False,
    task_acks_late=False,
    worker_prefetch_multiplier=1,
    task_default_queue="failure-workload",
)

EVIDENCE = Path(os.environ.get("CELERY_EVIDENCE_DIR", "/evidence"))
TASK_OBSERVATIONS = EVIDENCE / "task_observations.ndjson"
if os.environ.get("CELERY_ENABLE_TRACEMALLOC", "0") == "1" and tracemalloc is not None:
    tracemalloc.start(10)


def _emit_task_observation(event: str, **fields: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "worker_pid": os.getpid(),
        "repo_revision": os.environ.get("CELERY_REPO_REVISION", ""),
        **fields,
    }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with TASK_OBSERVATIONS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
    return payload


def _rss_bytes() -> int:
    page_size = os.sysconf("SC_PAGE_SIZE")
    with Path("/proc/self/statm").open(encoding="ascii") as handle:
        resident_pages = int(handle.read().split()[1])
    return resident_pages * page_size


def _memory_snapshot() -> dict[str, int]:
    snapshot = {"rss_bytes": _rss_bytes()}
    if tracemalloc is not None and tracemalloc.is_tracing():
        current, peak = tracemalloc.get_traced_memory()
        snapshot.update({"tracemalloc_current_bytes": current, "tracemalloc_peak_bytes": peak})
    return snapshot


@app.task(name="celery_case_tasks.unhandled_failure", ignore_result=True)
def unhandled_failure(sequence: int) -> None:
    """Raise through a nested frame so Celery handles an actual traceback."""

    payload = "x" * int(os.environ.get("CELERY_FAILURE_PAYLOAD_BYTES", "1"))
    task_seconds = float(os.environ.get("CELERY_FAILURE_TASK_SECONDS", "0"))
    if task_seconds > 0:
        time.sleep(task_seconds)
    try:
        _raise_from_nested_frame(sequence, payload)
    finally:
        _emit_task_observation("failure_task_finished", sequence=sequence)


def _raise_from_nested_frame(sequence: int, payload: str) -> None:
    if not payload:
        raise RuntimeError("empty failure payload")
    raise RuntimeError(f"unhandled-task-failure-{sequence}")


@app.task(name="celery_case_tasks.control_success", ignore_result=True)
def control_success(sequence: int) -> dict[str, int]:
    return {"sequence": sequence}


@app.task(name="celery_case_tasks.batch_barrier", ignore_result=True)
def batch_barrier(batch: int, submitted_failures: int) -> None:
    """Run after a submitted batch and record a worker-side measurement boundary."""

    collected = gc.collect()
    _emit_task_observation(
        "failure_batch_barrier",
        batch=batch,
        submitted_failures=submitted_failures,
        gc_collected=collected,
        **_memory_snapshot(),
    )


@app.task(name="celery_case_tasks.warmup_barrier", ignore_result=True)
def warmup_barrier(submitted_controls: int) -> None:
    gc.collect()
    _emit_task_observation(
        "warmup_barrier",
        submitted_controls=submitted_controls,
        **_memory_snapshot(),
    )
