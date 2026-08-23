FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --upgrade pip setuptools wheel "Cython~=3.0.5" "meson==1.2.1" "meson-python==0.13.1" ninja "numpy>=2.0.0rc1" "versioneer[toml]" python-dateutil pytz tzdata
WORKDIR /opt/project
COPY monitor.py /case/monitor.py
COPY pandas_groupby_workload.py /case/pandas_groupby_workload.py
COPY source /opt/project
RUN pip install --no-cache-dir --no-build-isolation /opt/project
WORKDIR /case
ENV PYTHONUNBUFFERED=1
CMD ["python", "/case/pandas_groupby_workload.py"]
