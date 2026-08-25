from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from celery_queue_tasks import app, delayed_noop, immediate_work
from case_lifecycle import CaseLifecycle


EVIDENCE = Path(os.environ.get("CASE_EVIDENCE_ROOT", "/evidence"))


def emit(event: str, **fields: object) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with (EVIDENCE / "workload.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, "observed_at": time.time(), **fields}, sort_keys=True) + "\n")


def wait_for_broker() -> None:
    app.connection_for_write().ensure_connection(max_retries=60, interval_start=0.2, interval_step=0.5, interval_max=2)


def wait_for_worker(proc: subprocess.Popen) -> None:
    inspector = app.control.inspect(timeout=1)
    for _ in range(90):
        if proc.poll() is not None:
            raise RuntimeError(f"worker exited early with code {proc.returncode}")
        try:
            if inspector.ping():
                return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("Celery worker did not respond to inspect ping")


def scheduled_count() -> int | None:
    try:
        payload = app.control.inspect(timeout=1).scheduled() or {}
    except Exception:
        return None
    total = 0
    for tasks in payload.values():
        total += len(tasks or [])
    return total


def main() -> None:
    wait_for_broker()
    worker = subprocess.Popen(
        [
            "celery", "-A", "celery_queue_tasks", "worker",
            "--loglevel=INFO", "--pool=solo", "--concurrency=1",
            "--queues=eta-workload", "--hostname=eta-worker@%h",
            "--without-gossip", "--without-mingle",
        ],
        cwd="/case",
    )
    submitted_eta = 0
    submitted_immediate = 0
    try:
        wait_for_worker(worker)
        (EVIDENCE / "ready").touch()
        eta_count = max(1, int(os.environ.get("CELERY_ETA_TASK_COUNT", "5000")))
        countdown = max(60, int(os.environ.get("CELERY_ETA_COUNTDOWN_SEC", "1800")))
        lifecycle = CaseLifecycle(EVIDENCE)
        emit("producer_start", eta_count=eta_count, countdown_sec=countdown)
        for sequence in range(1, eta_count + 1):
            delayed_noop.apply_async(args=[sequence], countdown=countdown, queue="eta-workload")
            submitted_eta = sequence
            if sequence % 250 == 0:
                emit("eta_submission_sample", submitted_eta=submitted_eta, scheduled_count=scheduled_count())
        while lifecycle.tick(emit) != "released":
            submitted_immediate += 1
            immediate_work.apply_async(args=[submitted_immediate], queue="eta-workload")
            if submitted_immediate % 10 == 0:
                emit(
                    "immediate_submission_sample",
                    submitted_eta=submitted_eta,
                    submitted_immediate=submitted_immediate,
                    scheduled_count=scheduled_count(),
                )
            time.sleep(0.5)
        emit("workload_complete", submitted_eta=submitted_eta, submitted_immediate=submitted_immediate, scheduled_count=scheduled_count())
        (EVIDENCE / "complete").touch()
    finally:
        worker.send_signal(signal.SIGTERM)
        try:
            worker.wait(timeout=20)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait(timeout=10)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        emit("workload_failed", error_type=type(exc).__name__, error=str(exc)[:500])
        raise
