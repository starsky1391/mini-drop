FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --upgrade pip "setuptools>=77.0" wheel "cython>=3.1.0,<4"
WORKDIR /opt/project
COPY monitor.py /case/monitor.py
COPY aiohttp_workload.py /case/aiohttp_workload.py
COPY source /opt/project
ENV AIOHTTP_NO_EXTENSIONS=1
RUN pip install --no-cache-dir /opt/project
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/opt/project
CMD ["python", "/case/aiohttp_workload.py"]
