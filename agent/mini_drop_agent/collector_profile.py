"""Agent-side collector capability profile discovery."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


def build_collector_profile(capabilities: list[str]) -> dict[str, Any]:
    checks = [
        _tool_profile("perf_cpu", "perf", "linux perf CPU sampling"),
        _tool_profile("ebpf_io", "bpftrace", "bpftrace eBPF IO latency"),
        _always_available("sys_metrics", "procfs system and process metrics"),
        _tool_profile("pyspy", "py-spy", "Python stack sampling"),
        _log_scan_profile(),
        _blackbox_profile(),
        _redis_exporter_profile(),
    ]
    by_family = {item["collector_type"]: item for item in checks}
    for capability in capabilities:
        by_family.setdefault(capability, _always_available(capability, "registered collector"))
    items = [by_family[key] for key in sorted(by_family)]
    return {
        "schema_version": "1.0",
        "managed": _env_bool("MINI_DROP_MANAGED_COLLECTORS", True),
        "summary": {
            "available": sum(1 for item in items if item["status"] == "available"),
            "degraded": sum(1 for item in items if item["status"] == "degraded"),
            "unavailable": sum(1 for item in items if item["status"] == "unavailable"),
        },
        "collectors": items,
    }


def collector_profile_json(capabilities: list[str]) -> str:
    return json.dumps(build_collector_profile(capabilities), ensure_ascii=False, sort_keys=True)


def _always_available(collector_type: str, source: str) -> dict[str, Any]:
    return {
        "collector_type": collector_type,
        "status": "available",
        "source": source,
        "reason": "registered",
        "default_options": {},
    }


def _tool_profile(collector_type: str, command: str, source: str) -> dict[str, Any]:
    path = shutil.which(command)
    return {
        "collector_type": collector_type,
        "status": "available" if path else "unavailable",
        "source": source,
        "reason": f"{command} found" if path else f"{command} command not found",
        "default_options": {},
    }


def _log_scan_profile() -> dict[str, Any]:
    source = os.getenv("MINI_DROP_LOG_PIPELINE_OUTPUT", "/var/lib/mini-drop/logs/logs.ndjson")
    paths = [item.strip() for item in source.split(",") if item.strip()]
    existing = [path for path in paths if Path(path).exists()]
    if existing:
        status = "available"
        reason = "industrial log pipeline output found"
    elif _env_bool("MINI_DROP_LOG_AUTO_DISCOVER", True) and any(Path(path).exists() for path in ("/var/log", "/var/lib/docker/containers")):
        status = "degraded"
        reason = "log input exists but normalized pipeline output is not ready yet"
    else:
        status = "unavailable"
        reason = "no log pipeline output or readable default log path"
    return {
        "collector_type": "log_scan",
        "status": status,
        "source": "Fluent Bit or OpenTelemetry filelog output",
        "reason": reason,
        "default_options": {"source_paths": paths},
    }


def _blackbox_profile() -> dict[str, Any]:
    url = os.getenv("MINI_DROP_BLACKBOX_URL", "http://blackbox-exporter:9115")
    status, reason = _http_ready(f"{url.rstrip('/')}/-/healthy")
    return {
        "collector_type": "dependency_check",
        "status": status,
        "source": "Prometheus Blackbox Exporter",
        "reason": reason,
        "default_options": {"blackbox_url": url},
    }


def _redis_exporter_profile() -> dict[str, Any]:
    url = os.getenv("MINI_DROP_REDIS_EXPORTER_URL", "")
    if not url:
        return {
            "collector_type": "redis_check",
            "status": "unavailable",
            "source": "Redis Exporter",
            "reason": "MINI_DROP_REDIS_EXPORTER_URL is not set",
            "default_options": {},
        }
    status, reason = _http_ready(url)
    return {
        "collector_type": "redis_check",
        "status": status,
        "source": "Redis Exporter",
        "reason": reason,
        "default_options": {"exporter_url": url},
    }


def _http_ready(url: str) -> tuple[str, str]:
    try:
        request = Request(url, headers={"Accept": "text/plain"})
        with urlopen(request, timeout=1.0) as response:  # noqa: S310 - URL is local/operator-configured.
            if 200 <= response.status < 500:
                return "available", f"endpoint reachable: {url}"
            return "degraded", f"endpoint returned HTTP {response.status}: {url}"
    except OSError:
        return "degraded", f"endpoint not reachable yet: {url}"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
