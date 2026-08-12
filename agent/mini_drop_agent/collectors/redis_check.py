"""Industrial Redis evidence adapter.

This collector consumes Redis Exporter / Prometheus metrics and normalizes them
into Mini-Drop's `redis_check_json` evidence contract.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from agent.mini_drop_agent.collectors.base import CollectorResult, CollectorTask


class RedisCheckCollector:
    """Normalize Redis Exporter metrics."""

    OUTPUT_BASE = "/tmp/mini-drop"
    DEFAULT_TIMEOUT_SEC = 5.0

    def collect(self, task: CollectorTask) -> CollectorResult:
        redis_target = _redis_target(task.options)
        if not redis_target and not _has_fixture_metrics(task.options):
            return CollectorResult(
                ok=False,
                reason="未提供本次任务的 Redis target_config，拒绝使用全局 Redis 目标",
            )
        metrics_text = _metrics_text(task.options)
        if metrics_text is None:
            return CollectorResult(
                ok=False,
                reason="未配置 Redis Exporter 输出: metrics_text/metrics_path/exporter_url",
            )

        output_dir = os.path.join(self.OUTPUT_BASE, task.id)
        os.makedirs(output_dir, exist_ok=True)
        metrics = _parse_prometheus_metrics(metrics_text)
        evidence_window = _evidence_window(task)
        output = {
            "schema_version": "1.0",
            "task_id": task.id,
            "collector_type": "redis_check",
            "collector_family": "redis_check",
            **_cohort_fields(task),
            "collector_invocation": _collector_invocation(task, redis_target),
            "adapter": {
                "kind": "redis_exporter_prometheus",
                "source": _adapter_source(task.options),
            },
            "target_pid": task.target_pid,
            "evidence_window": evidence_window,
            "target": redis_target,
            "connectivity": _connectivity(metrics),
            "info_summary": _info_summary(metrics),
            "slowlog_summary": _slowlog_summary(metrics),
            "latency_summary": _latency_summary(metrics),
            "evidence_refs": [
                "redis_check.connectivity",
                "redis_check.info_summary",
                "redis_check.slowlog_summary",
                "redis_check.latency_summary",
            ],
        }
        output_path = os.path.join(output_dir, "redis_check.json")
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(output, fh, ensure_ascii=False, indent=2)
        return CollectorResult(
            ok=True,
            reason=(
                "Redis Exporter 适配完成: "
                f"up={output['connectivity']['exporter_up']}, "
                f"connected_clients={output['info_summary'].get('connected_clients', 0)}"
            ),
            artifacts=[{
                "artifact_type": "redis_check_json",
                "filename": "redis_check.json",
                "local_path": output_path,
                "content_type": "application/json",
                "size_bytes": os.path.getsize(output_path),
                "collector_family": "redis_check",
                "evidence_window": evidence_window,
                **_cohort_fields(task),
                "metadata": {
                    "exporter_up": output["connectivity"]["exporter_up"],
                    "connected_clients": output["info_summary"].get("connected_clients", 0),
                    "slowlog_entry_count": output["slowlog_summary"]["entry_count"],
                    "max_latency_ms": output["latency_summary"]["max_latency_ms"],
                },
            }],
        )


def _metrics_text(options: dict[str, Any]) -> str | None:
    inline = options.get("metrics_text") or options.get("redis_exporter_metrics")
    if isinstance(inline, str) and inline.strip():
        return inline
    path = options.get("metrics_path")
    if path:
        try:
            return open(str(path), encoding="utf-8").read()
        except (FileNotFoundError, PermissionError, OSError, UnicodeDecodeError):
            return None
    url = options.get("exporter_url") or options.get("redis_exporter_url") or os.getenv("MINI_DROP_REDIS_EXPORTER_URL", "")
    if url:
        redis_target = _redis_target(options)
        if redis_target.get("url") and not _looks_like_metrics_url(str(url)):
            query = urlencode({"target": redis_target["url"]})
            url = f"{str(url).rstrip('/')}/scrape?{query}"
        timeout = _safe_float(options.get("timeout_sec"), RedisCheckCollector.DEFAULT_TIMEOUT_SEC)
        try:
            request = Request(str(url), headers={"Accept": "text/plain"})
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 - endpoint is operator-configured.
                return response.read().decode("utf-8", errors="replace")
        except OSError:
            return None
    return None


def _has_fixture_metrics(options: dict[str, Any]) -> bool:
    return isinstance(options.get("metrics_text") or options.get("redis_exporter_metrics"), str) or bool(options.get("metrics_path"))


def _redis_target(options: dict[str, Any]) -> dict[str, Any]:
    target_config = options.get("target_config")
    if isinstance(target_config, dict) and isinstance(target_config.get("redis_target"), dict):
        raw = target_config["redis_target"]
    else:
        raw = options
    url = raw.get("url") or raw.get("redis_url")
    host = raw.get("host") or raw.get("redis_host")
    port = raw.get("port") or raw.get("redis_port")
    if not url and host:
        port = int(port or 6379)
        url = f"redis://{host}:{port}"
    if not url and not host:
        return {}
    return {
        "dependency_id": raw.get("dependency_id") or raw.get("instance"),
        "protocol": "redis",
        "host": host or _host_from_redis_url(str(url)),
        "port": int(port or _port_from_redis_url(str(url)) or 6379),
        "url": url,
    }


def _collector_invocation(task: CollectorTask, redis_target: dict[str, Any]) -> dict[str, Any]:
    invocation = task.options.get("collector_invocation")
    if isinstance(invocation, dict):
        return invocation
    return {
        "schema_version": "1.0",
        "scope_source": "task_options",
        "collector_family": "redis_check",
        "target_config": {"redis_target": redis_target},
    }


def _looks_like_metrics_url(url: str) -> bool:
    return url.rstrip("/").endswith("/metrics") or "/scrape?" in url


def _host_from_redis_url(url: str) -> str:
    if "://" not in url:
        return ""
    try:
        from urllib.parse import urlparse

        return urlparse(url).hostname or ""
    except Exception:
        return ""


def _port_from_redis_url(url: str) -> int:
    if "://" not in url:
        return 0
    try:
        from urllib.parse import urlparse

        return int(urlparse(url).port or 0)
    except (TypeError, ValueError):
        return 0


def _parse_prometheus_metrics(text: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.rsplit(None, 1)
        if len(parts) != 2:
            continue
        name = parts[0].split("{", 1)[0]
        try:
            result[name] = float(parts[1])
        except ValueError:
            continue
    return result


def _connectivity(metrics: dict[str, float]) -> dict[str, Any]:
    exporter_up = metrics.get("redis_up", metrics.get("up", 0.0)) == 1.0
    return {
        "exporter_up": exporter_up,
        "ping_ok": exporter_up,
        "latency_ms": round(_first_metric(metrics, ("redis_exporter_last_scrape_duration_seconds", "scrape_duration_seconds")) * 1000, 2),
        "error_type": "" if exporter_up else "redis_exporter_down_or_redis_unreachable",
        "evidence_ref": "redis_check.connectivity",
    }


def _info_summary(metrics: dict[str, float]) -> dict[str, Any]:
    return {
        "connected_clients": int(_first_metric(metrics, ("redis_connected_clients",))),
        "blocked_clients": int(_first_metric(metrics, ("redis_blocked_clients",))),
        "used_memory_bytes": int(_first_metric(metrics, ("redis_memory_used_bytes", "redis_memory_used"))),
        "evicted_keys_total": int(_first_metric(metrics, ("redis_evicted_keys_total", "redis_evicted_keys"))),
        "rejected_connections_total": int(_first_metric(metrics, ("redis_rejected_connections_total", "redis_rejected_connections"))),
        "total_error_replies": int(_first_metric(metrics, ("redis_total_error_replies",))),
        "instantaneous_ops_per_sec": int(_first_metric(metrics, ("redis_commands_processed_total",))),
        "evidence_ref": "redis_check.info_summary",
    }


def _slowlog_summary(metrics: dict[str, float]) -> dict[str, Any]:
    entry_count = int(_first_metric(metrics, ("redis_slowlog_length",)))
    max_duration = int(_first_metric(metrics, ("redis_slowlog_last_duration_seconds",)) * 1_000_000)
    return {
        "entry_count": entry_count,
        "max_duration_us": max_duration,
        "top_commands": [],
        "evidence_ref": "redis_check.slowlog_summary",
    }


def _latency_summary(metrics: dict[str, float]) -> dict[str, Any]:
    max_latency_ms = round(_first_metric(
        metrics,
        ("redis_latency_spike_last_ms", "redis_commands_duration_seconds_total", "redis_exporter_last_scrape_duration_seconds"),
    ) * (1 if "redis_latency_spike_last_ms" in metrics else 1000), 2)
    return {
        "max_latency_ms": max_latency_ms,
        "events": [],
        "evidence_ref": "redis_check.latency_summary",
    }


def _first_metric(metrics: dict[str, float], names: tuple[str, ...]) -> float:
    for name in names:
        if name in metrics:
            return metrics[name]
    return 0.0


def _adapter_source(options: dict[str, Any]) -> str:
    if options.get("exporter_url") or options.get("redis_exporter_url"):
        return "redis_exporter_http_metrics"
    if options.get("metrics_path"):
        return "redis_exporter_metrics_file"
    return "redis_exporter_metrics_inline"


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _cohort_fields(task: CollectorTask) -> dict[str, Any]:
    return {
        "trigger_event_id": task.options.get("trigger_event_id"),
        "evidence_cohort_id": task.options.get("evidence_cohort_id"),
        "collection_mode": task.options.get("collection_mode") or "manual_single",
        "timing_relation": task.options.get("timing_relation") or "unknown",
    }


def _evidence_window(task: CollectorTask) -> dict[str, Any]:
    return {
        "window_start": task.options.get("window_start"),
        "window_end": task.options.get("window_end"),
        "duration_sec": task.duration_sec,
        **_cohort_fields(task),
    }
