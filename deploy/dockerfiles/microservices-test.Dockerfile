# Mini-Drop microservices test cluster image
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir fastapi uvicorn requests

WORKDIR /app
COPY microservices_test/ /app/microservices_test/

EXPOSE 8080

