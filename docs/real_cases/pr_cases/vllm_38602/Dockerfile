FROM python:3.11-slim
WORKDIR /opt/project
COPY monitor.py /case/monitor.py
COPY case_lifecycle.py /case/case_lifecycle.py
COPY vllm_workload.py /case/vllm_workload.py
COPY source /opt/project
RUN pip install --no-cache-dir -e /opt/project
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/opt/project
CMD ["python", "/case/vllm_workload.py"]
