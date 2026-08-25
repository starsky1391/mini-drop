from __future__ import annotations

import io
import json
import os
import threading
import time
from pathlib import Path

import av

from case_lifecycle import CaseLifecycle


EVIDENCE = Path(os.environ.get("CASE_EVIDENCE_ROOT", "/evidence"))


def emit(event: str, **fields: object) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with (EVIDENCE / "workload.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, "observed_at": time.time(), **fields}) + "\n")


def worker() -> None:
    for _ in range(1000):
        try:
            with av.logging.Capture(local=True):
                av.open(io.BytesIO(b"not-a-media-file"))
        except Exception:
            pass


def main() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    av.logging.set_level(av.logging.VERBOSE)
    (EVIDENCE / "ready").touch()
    lifecycle = CaseLifecycle(EVIDENCE)
    while lifecycle.tick(emit) != "released":
        threads = [threading.Thread(target=worker, daemon=True) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        emit("threaded_logging_cycle", alive_threads=sum(thread.is_alive() for thread in threads))
    av.logging.set_level(None)
    (EVIDENCE / "complete").touch()


main()
