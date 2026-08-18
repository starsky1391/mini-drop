"""Small JSON logging helpers for the server runtime."""

from __future__ import annotations

import json
import sys
import time
from typing import Any


def log_event(level: str, event: str, **fields: Any) -> None:
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": level,
        "event": event,
        **fields,
    }
    stream = sys.stderr if level in {"error", "warning"} else sys.stdout
    print(json.dumps(record, ensure_ascii=False, default=str), file=stream, flush=True)
