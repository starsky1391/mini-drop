from __future__ import annotations

import os
import time

from celery import Celery


app = Celery("eta_queue_workload", broker=os.environ.get("CELERY_BROKER_URL", "redis://dependency:6379/0"))
app.conf.update(
    task_ignore_result=True,
    task_acks_late=False,
    worker_prefetch_multiplier=1,
    task_default_queue="eta-workload",
    broker_connection_retry_on_startup=True,
)


@app.task(name="celery_queue_tasks.delayed_noop", ignore_result=True)
def delayed_noop(sequence: int) -> int:
    return sequence


@app.task(name="celery_queue_tasks.immediate_work", ignore_result=True)
def immediate_work(sequence: int) -> int:
    time.sleep(float(os.environ.get("CELERY_IMMEDIATE_TASK_SECONDS", "0.05")))
    return sequence
