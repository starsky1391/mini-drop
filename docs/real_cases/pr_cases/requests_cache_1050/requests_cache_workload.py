from __future__ import annotations

import json
import os
import pickle
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests_cache
from requests_cache.serializers.pipeline import SerializerPipeline, Stage


EVIDENCE = Path(os.environ.get("CASE_EVIDENCE_ROOT", "/evidence"))
CACHE_ROOT = Path(os.environ.get("CACHE_ROOT", "/evidence/http-cache"))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = f"path={self.path}".encode()
        self.send_response(200)
        self.send_header("Cache-Control", "public, max-age=3600")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def emit(event: str, **fields: object) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with (EVIDENCE / "workload.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, "observed_at": time.time(), **fields}, sort_keys=True) + "\n")


def cache_stats() -> dict[str, int]:
    if not CACHE_ROOT.exists():
        return {"cache_files": 0, "cache_bytes": 0}
    files = [p for p in CACHE_ROOT.rglob("*") if p.is_file()]
    return {"cache_files": len(files), "cache_bytes": sum(p.stat().st_size for p in files)}


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 18080), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    raw_pickle_serializer = SerializerPipeline([Stage(pickle)], name="pickle", is_binary=True)
    session = requests_cache.CachedSession(
        cache_name=str(CACHE_ROOT / "case-cache"),
        backend="filesystem",
        expire_after=3600,
        serializer=raw_pickle_serializer,
    )
    deadline = time.monotonic() + max(30, int(os.environ.get("CASE_DURATION_SEC", "180")))
    count = 0
    (EVIDENCE / "ready").touch()
    try:
        while time.monotonic() < deadline:
            url = f"http://127.0.0.1:18080/item/{count}?variant={count}"
            response = session.get(url, timeout=2)
            count += 1
            if count % 100 == 0:
                emit("cache_batch", count=count, status=response.status_code, from_cache=bool(getattr(response, "from_cache", False)), **cache_stats())
        emit("workload_complete", count=count, **cache_stats())
        (EVIDENCE / "complete").touch()
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
