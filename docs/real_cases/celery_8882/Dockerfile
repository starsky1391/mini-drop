FROM python:3.11-slim

ARG CELERY_REVISION=a83070e5ec748c32325332db422756cfdd709aae

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc git procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /case
COPY celery-src /opt/celery-src
RUN test "$(git -C /opt/celery-src rev-parse HEAD)" = "${CELERY_REVISION}" \
    && pip install --no-cache-dir -e "/opt/celery-src[redis]"

COPY celery_case_tasks.py producer.py worker_entrypoint.py worker_monitor.py ./

ENV PYTHONUNBUFFERED=1 \
    CELERY_SOURCE_ROOT=/opt/celery-src \
    CELERY_CONTAINER_WORKDIR=/opt/celery-src \
    CELERY_REPO_REVISION=${CELERY_REVISION} \
    CELERY_BROKER_URL=redis://redis:6379/0

CMD ["python", "/case/worker_entrypoint.py"]
