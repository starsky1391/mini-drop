FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends gcc procps && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --upgrade pip setuptools wheel
WORKDIR /opt/project
COPY monitor.py /case/monitor.py
COPY celery_queue_tasks.py /case/celery_queue_tasks.py
COPY celery_queue_workload.py /case/celery_queue_workload.py
COPY source /opt/project
RUN pip install --no-cache-dir -e "/opt/project[redis]"
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/case:/opt/project C_FORCE_ROOT=1
CMD ["python", "/case/celery_queue_workload.py"]
