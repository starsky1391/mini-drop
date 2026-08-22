from __future__ import annotations

import json
import os
import time
from pathlib import Path


EVIDENCE = Path(os.environ.get("CASE_EVIDENCE_ROOT", "/evidence"))


def emit(event: str, **fields: object) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with (EVIDENCE / "workload.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, "observed_at": time.time(), **fields}) + "\n")


def main() -> None:
    import ray

    (EVIDENCE / "ready").touch()
    ray.init(address=os.environ.get("RAY_ADDRESS", "auto"), ignore_reinit_error=True)
    emit("ray_connected")
    emit("logprobs_workload_requires_gpu", gpu_required=True)
    deadline = time.monotonic() + max(30, int(os.environ.get("CASE_DURATION_SEC", "180")))
    while time.monotonic() < deadline:
        time.sleep(1)
    (EVIDENCE / "complete").touch()


main()
