from __future__ import annotations

import inspect
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import urllib3
from urllib3.util import Retry


EVIDENCE = Path(os.environ.get("CASE_EVIDENCE_ROOT", "/evidence"))


class FailingHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"temporarily unavailable"
        self.send_response(503)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def emit(event: str, **fields: object) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with (EVIDENCE / "workload.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, "observed_at": time.time(), **fields}, sort_keys=True) + "\n")


def make_retry() -> Retry:
    kwargs = {
        "total": 5,
        "status": 5,
        "backoff_factor": 0.2,
        "status_forcelist": [503],
        "raise_on_status": True,
    }
    if "backoff_max" in inspect.signature(Retry.__init__).parameters:
        kwargs["backoff_max"] = 0.2
    return Retry(**kwargs)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 18080), FailingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    retries = make_retry()
    pool = urllib3.PoolManager(retries=retries, timeout=urllib3.Timeout(connect=1.0, read=1.0))
    emit("retry_configuration", retry_kwargs=repr(retries))
    (EVIDENCE / "ready").touch()
    deadline = time.monotonic() + max(30, int(os.environ.get("CASE_DURATION_SEC", "180")))
    count = 0
    errors = 0
    try:
        while time.monotonic() < deadline:
            started = time.perf_counter()
            try:
                pool.request("GET", "http://127.0.0.1:18080/flaky")
            except Exception as exc:
                errors += 1
                emit("retry_request_error", count=count, errors=errors, error_type=type(exc).__name__, elapsed_ms=(time.perf_counter() - started) * 1000)
            count += 1
    finally:
        emit("workload_complete", count=count, errors=errors)
        (EVIDENCE / "complete").touch()
        server.shutdown()


if __name__ == "__main__":
    main()
