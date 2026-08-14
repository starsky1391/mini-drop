"""Low-cost process observations for Persistent Watch leases."""

from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from server.app.generated import watch_pb2


class WatchObservationState:
    """Keep a bounded per-watch window without retaining raw process data."""

    def __init__(self, max_samples: int = 120) -> None:
        self._samples: deque[dict[str, Any]] = deque(maxlen=max_samples)

    def append(self, sample: dict[str, Any]) -> None:
        self._samples.append(sample)

    def split(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if len(self._samples) < 6:
            return [], []
        values = list(self._samples)
        return values[:-3], values[-3:]


@dataclass(frozen=True)
class _PidSnapshot:
    monotonic_ts: float
    cpu_seconds: float


class ProcessDeltaSampler:
    """Per-PID delta sampler for short-window CPU signals."""

    def __init__(self) -> None:
        self._last: dict[int, _PidSnapshot] = {}
        self._clock_ticks = _sysconf("SC_CLK_TCK", 100)
        self._page_size = _sysconf("SC_PAGE_SIZE", 4096)

    def read(self, pid: int) -> dict[str, float] | None:
        current = _read_pid_snapshot(pid, self._clock_ticks, self._page_size)
        if current is None:
            self._last.pop(pid, None)
            return None
        metrics = dict(current["metrics"])
        snapshot = current["snapshot"]
        previous = self._last.get(pid)
        self._last[pid] = snapshot
        if previous is not None:
            elapsed = max(snapshot.monotonic_ts - previous.monotonic_ts, 0.001)
            cpu_delta = max(snapshot.cpu_seconds - previous.cpu_seconds, 0.0)
            metrics["cpu_percent"] = cpu_delta / elapsed * 100.0
        return metrics


def observe_watch_lease(
    lease: watch_pb2.WatchLease,
    sampler: ProcessDeltaSampler | None = None,
) -> tuple[watch_pb2.WatchMetricSample, bool, str]:
    observed_at = datetime.now(timezone.utc).isoformat()
    pid = int(lease.target_pid)
    if not _pid_exists(pid):
        return (
            watch_pb2.WatchMetricSample(observed_at=observed_at),
            False,
            "target_exit",
        )

    metrics = sampler.read(pid) if sampler is not None else _read_pid_metrics(pid)
    metrics = metrics or {}
    sample = watch_pb2.WatchMetricSample(observed_at=observed_at)
    _set_metric(sample, "cpu_percent", metrics.get("cpu_percent"))
    _set_metric(sample, "thread_count", metrics.get("thread_count"))
    _set_metric(sample, "rss_mb", metrics.get("rss_mb"))
    _set_metric(sample, "iowait_percent", _read_system_iowait())
    return sample, True, "ok"


def sample_to_dict(sample: watch_pb2.WatchMetricSample) -> dict[str, Any]:
    values: dict[str, Any] = {
        "observed_at": sample.observed_at,
    }
    for field, present in (
        ("cpu_percent", sample.has_cpu_percent),
        ("latency_ms_p99", sample.has_latency_ms_p99),
        ("thread_count", sample.has_thread_count),
        ("iowait_percent", sample.has_iowait_percent),
        ("rss_mb", sample.has_rss_mb),
        ("error_count", sample.has_error_count),
    ):
        if present:
            values[field] = getattr(sample, field)
    return values


def dict_to_sample(values: dict[str, Any]) -> watch_pb2.WatchMetricSample:
    sample = watch_pb2.WatchMetricSample(observed_at=str(values.get("observed_at", "")))
    for field in (
        "cpu_percent",
        "latency_ms_p99",
        "thread_count",
        "iowait_percent",
        "rss_mb",
        "error_count",
    ):
        if field in values and values[field] is not None:
            _set_metric(sample, field, values[field])
    return sample


def _set_metric(sample: watch_pb2.WatchMetricSample, field: str, value: Any) -> None:
    if value is None:
        return
    setattr(sample, field, float(value))
    setattr(sample, f"has_{field}", True)


def _pid_exists(pid: int) -> bool:
    return os.path.isdir(f"/proc/{pid}")


def _read_pid_metrics(pid: int) -> dict[str, float]:
    snapshot = _read_pid_snapshot(pid, _sysconf("SC_CLK_TCK", 100), _sysconf("SC_PAGE_SIZE", 4096))
    if snapshot is None:
        return {}
    metrics = dict(snapshot["metrics"])
    age = max(_system_uptime() - snapshot["start_ticks"] / max(_sysconf("SC_CLK_TCK", 100), 1), 0.001)
    metrics["cpu_percent"] = max(0.0, snapshot["snapshot"].cpu_seconds / age * 100.0)
    return metrics


def _read_pid_snapshot(pid: int, clock_ticks: int, page_size: int) -> dict[str, Any] | None:
    result: dict[str, float] = {}
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as fh:
            text = fh.read()
        closing = text.rfind(")")
        fields = text[closing + 2 :].split()
        utime = int(fields[11])
        stime = int(fields[12])
        start_ticks = int(fields[19])
        if len(fields) >= 20:
            result["thread_count"] = float(fields[17])
        if len(fields) >= 24:
            rss_pages = int(fields[21])
            result["rss_mb"] = rss_pages * page_size / 1024 / 1024
    except (OSError, ValueError, IndexError):
        return None
    return {
        "metrics": result,
        "snapshot": _PidSnapshot(
            monotonic_ts=time.monotonic(),
            cpu_seconds=(utime + stime) / max(clock_ticks, 1),
        ),
        "start_ticks": start_ticks,
    }


def _system_uptime() -> float:
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as fh:
            return float(fh.readline().split()[0])
    except (OSError, ValueError, IndexError):
        return time.monotonic()


def _sysconf(name: str, default: int) -> int:
    try:
        return int(os.sysconf(name))
    except (AttributeError, OSError, ValueError):
        return default


def _read_system_iowait() -> float | None:
    try:
        with open("/proc/stat", "r", encoding="utf-8") as fh:
            line = next(line for line in fh if line.startswith("cpu "))
        fields = [int(value) for value in line.split()[1:]]
        total = sum(fields)
        return fields[4] / total * 100.0 if total else 0.0
    except (OSError, ValueError, IndexError, StopIteration):
        return None
