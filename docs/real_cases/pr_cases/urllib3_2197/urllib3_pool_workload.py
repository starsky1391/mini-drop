from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import urllib3


EVIDENCE = Path(os.environ.get("CASE_EVIDENCE_ROOT", "/evidence"))


class SlowHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"x" * 1024
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        time.sleep(2.0)
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def emit(event: str, **fields: object) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with (EVIDENCE / "workload.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, "observed_at": time.time(), **fields}, sort_keys=True) + "\n")


def worker(pool: urllib3.PoolManager, stop_at: float, worker_id: int) -> None:
    successes = 0
    failures = 0
    while time.monotonic() < stop_at:
        started = time.perf_counter()
        try:
            response = pool.request(
                "GET",
                "http://127.0.0.1:18080/slow",
                preload_content=False,
                pool_timeout=0.25,
                timeout=urllib3.Timeout(connect=1.0, read=5.0),
            )
            try:
                time.sleep(1.0)
                response.read()
                successes += 1
            finally:
                response.release_conn()
        except Exception as exc:
            failures += 1
            emit("pool_request_error", worker_id=worker_id, error_type=type(exc).__name__, elapsed_ms=(time.perf_counter() - started) * 1000)
        if (successes + failures) % 10 == 0:
            emit("pool_worker_sample", worker_id=worker_id, successes=successes, failures=failures)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 18080), SlowHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    deadline = time.monotonic() + max(30, int(os.environ.get("CASE_DURATION_SEC", "180")))
    pool = urllib3.PoolManager(num_pools=1, maxsize=2, block=True)
    threads = [threading.Thread(target=worker, args=(pool, deadline, i), daemon=True) for i in range(8)]
    (EVIDENCE / "ready").touch()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    emit("workload_complete", workers=len(threads))
    (EVIDENCE / "complete").touch()
    server.shutdown()


if __name__ == "__main__":
    main()
