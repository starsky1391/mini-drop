FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates chromium libnss3 libatk-bridge2.0-0 libgtk-3-0 libgbm1 \
    libasound2 libx11-xcb1 libxcomposite1 libxdamage1 libxrandr2 \
    libxshmfence1 libdrm2 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir playwright==1.58.0
WORKDIR /runtime/source
COPY monitor.py /case/monitor.py
COPY playwright_workload.py /case/playwright_workload.py
COPY source /runtime/source
RUN cp -a /usr/local/lib/python3.11/site-packages/playwright/driver /runtime/source/playwright/driver
RUN cp /usr/local/lib/python3.11/site-packages/playwright/_repo_version.py /runtime/source/playwright/_repo_version.py

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/runtime/source
CMD ["python", "/case/playwright_workload.py"]
