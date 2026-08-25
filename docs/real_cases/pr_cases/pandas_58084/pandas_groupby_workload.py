from __future__ import annotations

import json
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd

from case_lifecycle import CaseLifecycle


EVIDENCE = Path(os.environ.get("CASE_EVIDENCE_ROOT", "/evidence"))


def emit(event: str, **fields: object) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    with (EVIDENCE / "workload.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, "observed_at": time.time(), **fields}, sort_keys=True) + "\n")


def build_frame(rows: int = 50000, categories: int = 5000) -> pd.DataFrame:
    observed = np.arange(rows) % 100
    dtype = pd.CategoricalDtype(categories=[f"cat-{i}" for i in range(categories)])
    return pd.DataFrame({
        "group": pd.Series([f"cat-{i}" for i in observed], dtype=dtype),
        "value": np.arange(rows, dtype="int64"),
    })


def main() -> None:
    frame = build_frame()
    (EVIDENCE / "ready").touch()
    lifecycle = CaseLifecycle(EVIDENCE)
    count = 0
    while lifecycle.tick(emit) != "released":
        started = time.perf_counter()
        result = frame.groupby("group", observed=False)["value"].transform("sum")
        elapsed_ms = (time.perf_counter() - started) * 1000
        count += 1
        if count == 1 or count % 100 == 0:
            emit("groupby_transform_sample", count=count, elapsed_ms=elapsed_ms, rows=len(frame), categories=len(frame["group"].cat.categories), checksum=int(result.iloc[0]))
    emit("workload_complete", count=count)
    (EVIDENCE / "complete").touch()


if __name__ == "__main__":
    main()
