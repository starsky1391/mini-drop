FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --upgrade pip setuptools wheel
WORKDIR /opt/project
COPY monitor.py /case/monitor.py
COPY case_lifecycle.py /case/case_lifecycle.py
COPY urllib3_retry_workload.py /case/urllib3_retry_workload.py
COPY source /opt/project
RUN pip install --no-cache-dir /opt/project
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/opt/project
CMD ["python", "/case/urllib3_retry_workload.py"]
