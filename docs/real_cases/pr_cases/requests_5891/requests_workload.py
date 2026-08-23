from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests


EVIDENCE = Path(os.environ.get("CASE_EVIDENCE_ROOT", "/evidence"))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def emit(event: str, **fields: object) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with (EVIDENCE / "workload.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, "observed_at": time.time(), **fields}, sort_keys=True) + "\n")


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 18080), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session = requests.Session()
    deadline = time.monotonic() + max(30, int(os.environ.get("CASE_DURATION_SEC", "180")))
    count = 0
    started = time.perf_counter()
    (EVIDENCE / "ready").touch()
    try:
        while time.monotonic() < deadline:
            response = session.get("http://127.0.0.1:18080/", timeout=2)
            count += 1
            if count % 100 == 0:
                elapsed = max(0.001, time.perf_counter() - started)
                emit("request_batch", count=count, rate_per_sec=count / elapsed, status=response.status_code)
    finally:
        emit("workload_complete", count=count)
        (EVIDENCE / "complete").touch()
        server.shutdown()


if __name__ == "__main__":
    main()
