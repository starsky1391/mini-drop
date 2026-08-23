FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --upgrade pip setuptools wheel "cython<3" numpy python-dateutil pytz tzdata
WORKDIR /opt/project
COPY monitor.py /case/monitor.py
COPY pandas_groupby_workload.py /case/pandas_groupby_workload.py
COPY source /opt/project
RUN pip install --no-cache-dir --no-build-isolation /opt/project
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/opt/project
CMD ["python", "/case/pandas_groupby_workload.py"]
