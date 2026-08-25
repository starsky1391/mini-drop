from __future__ import annotations

import json
import os
import time
from pathlib import Path

from kafka.admin import KafkaAdminClient, NewTopic

from case_lifecycle import CaseLifecycle


EVIDENCE = Path(os.environ.get("CASE_EVIDENCE_ROOT", "/evidence"))


def emit(event: str, **fields: object) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with (EVIDENCE / "workload.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, "observed_at": time.time(), **fields}) + "\n")


def main() -> None:
    lifecycle = CaseLifecycle(EVIDENCE)
    bootstrap = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "dependency:9092")
    ready = False
    while lifecycle.tick(emit) != "released":
        admin = None
        try:
            admin = KafkaAdminClient(bootstrap_servers=bootstrap, request_timeout_ms=3000, api_version_auto_timeout_ms=3000)
            name = f"mini-drop-{int(time.time() * 1000)}"
            admin.list_topics()
            admin.create_topics([NewTopic(name, num_partitions=1, replication_factor=1)], validate_only=False)
            admin.delete_topics([name])
            emit("admin_cycle", topic=name)
        except Exception as exc:
            emit("admin_error", error=type(exc).__name__)
        finally:
            if not ready:
                (EVIDENCE / "ready").touch()
                ready = True
            if admin is not None:
                admin.close()
    (EVIDENCE / "complete").touch()


main()
