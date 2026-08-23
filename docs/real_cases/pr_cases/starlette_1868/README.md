# Starlette #1868 / #1940

The workload runs a real Starlette application with `BaseHTTPMiddleware` around a streaming endpoint and drives it with `httpx`. It is intended for endpoint latency and request-path source localization without exposing PR details to the Analyzer.
