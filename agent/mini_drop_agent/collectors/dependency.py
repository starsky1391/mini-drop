"""Industrial dependency probe adapter.

Mini-Drop does not implement DNS/TCP/HTTP/gRPC probing here. This collector
adapts Prometheus Blackbox Exporter `/probe` results into `dependency_check_json`.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from agent.mini_drop_agent.collectors.base import CollectorResult, CollectorTask


class DependencyCheckCollector:
    """Normalize Blackbox Exporter probe metrics."""

    OUTPUT_BASE = "/tmp/mini-drop"
    DEFAULT_TIMEOUT_SEC = 5.0

    def collect(self, task: CollectorTask) -> CollectorResult:
        targets = _normalize_targets(task.options.get("targets") or task.options.get("dependencies"))
        if not targets:
            return CollectorResult(ok=False, reason="未提供 dependency_check targets")

        output_dir = os.path.join(self.OUTPUT_BASE, task.id)
        os.makedirs(output_dir, exist_ok=True)
        timeout = _safe_float(task.options.get("timeout_sec"), self.DEFAULT_TIMEOUT_SEC)
        evidence_window = _evidence_window(task)
        checks = []
        for target in targets[:20]:
            metrics_text = _metrics_for_target(task.options, target, timeout)
            if metrics_text is None:
                checks.append(_failed_adapter_check(target, "blackbox_exporter_output_unavailable"))
            else:
                checks.append(_blackbox_metrics_to_check(target, metrics_text))

        summary = {
            "total_targets": len(checks),
            "failed_dependencies": [item["dependency_id"] for item in checks if not item["success"]],
            "dns_failures": sum(1 for item in checks if item["failure_phase"] == "dns"),
            "tcp_failures": sum(1 for item in checks if item["failure_phase"] == "tcp"),
            "http_failures": sum(1 for item in checks if item["failure_phase"] == "http"),
            "tls_failures": sum(1 for item in checks if item["failure_phase"] == "tls"),
            "adapter_failures": sum(1 for item in checks if item["failure_phase"] == "adapter"),
        }
        output = {
            "schema_version": "1.0",
            "task_id": task.id,
            "collector_type": "dependency_check",
            "collector_family": "dependency_check",
            **_cohort_fields(task),
            "collector_invocation": _collector_invocation(task),
            "adapter": {
                "kind": "prometheus_blackbox_exporter",
                "source": _adapter_source(task.options),
            },
            "target_pid": task.target_pid,
            "evidence_window": evidence_window,
            "checks": checks,
            "summary": summary,
        }
        output_path = os.path.join(output_dir, "dependency_check.json")
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(output, fh, ensure_ascii=False, indent=2)
        return CollectorResult(
            ok=True,
            reason=f"Blackbox 依赖适配完成: {len(checks)} 个目标, 失败 {len(summary['failed_dependencies'])} 个",
            artifacts=[{
                "artifact_type": "dependency_check_json",
                "filename": "dependency_check.json",
                "local_path": output_path,
                "content_type": "application/json",
                "size_bytes": os.path.getsize(output_path),
                "collector_family": "dependency_check",
                "evidence_window": evidence_window,
                **_cohort_fields(task),
                "metadata": summary,
            }],
        )


def _metrics_for_target(options: dict[str, Any], target: dict[str, Any], timeout: float) -> str | None:
    inline = target.get("blackbox_metrics") or target.get("metrics_text")
    if isinstance(inline, str) and inline.strip():
        return inline
    metrics_path = target.get("metrics_path") or options.get("metrics_path")
    if metrics_path:
        try:
            return open(str(metrics_path), encoding="utf-8").read()
        except (FileNotFoundError, PermissionError, OSError, UnicodeDecodeError):
            return None
    endpoint = options.get("blackbox_url") or options.get("blackbox_endpoint") or os.getenv("MINI_DROP_BLACKBOX_URL", "http://blackbox-exporter:9115")
    probe_target = _blackbox_probe_target(target)
    if not endpoint or not probe_target:
        return None
    module = target.get("module") or options.get("module") or _default_module(target)
    query = urlencode({"target": probe_target, "module": module})
    url = f"{str(endpoint).rstrip('/')}/probe?{query}"
    try:
        request = Request(url, headers={"Accept": "text/plain"})
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - endpoint is operator-configured.
            return response.read().decode("utf-8", errors="replace")
    except OSError:
        return None


def _blackbox_metrics_to_check(target: dict[str, Any], metrics_text: str) -> dict[str, Any]:
    metrics = _parse_prometheus_metrics(metrics_text)
    success = metrics.get("probe_success", 0.0) == 1.0
    dns_duration = metrics.get('probe_dns_lookup_time_seconds', 0.0)
    tcp_duration = metrics.get('probe_tcp_connect_duration_seconds', 0.0)
    tls_duration = metrics.get('probe_tls_handshake_duration_seconds', 0.0)
    http_status = int(metrics.get("probe_http_status_code", 0.0))
    duration = metrics.get("probe_duration_seconds", 0.0)
    failure_phase = ""
    if not success:
        if dns_duration <= 0:
            failure_phase = "dns"
        elif tcp_duration <= 0:
            failure_phase = "tcp"
        elif _is_tls_target(target) and tls_duration <= 0:
            failure_phase = "tls"
        elif http_status >= 500 or http_status == 0:
            failure_phase = "http"
        else:
            failure_phase = "unknown"
    return {
        "dependency_id": str(target.get("dependency_id") or target.get("name") or target.get("url") or _address(target) or "dependency"),
        "protocol": str(target.get("protocol") or _protocol_from_target(target)),
        "target": target.get("url") or _address(target),
        "success": success,
        "duration_ms": round(duration * 1000, 2),
        "dns_duration_ms": round(dns_duration * 1000, 2),
        "tcp_connect_duration_ms": round(tcp_duration * 1000, 2),
        "tls_handshake_duration_ms": round(tls_duration * 1000, 2),
        "http_status_code": http_status or None,
        "failure_phase": failure_phase,
        "error_type": "" if success else f"blackbox_{failure_phase or 'probe'}_failure",
        "evidence_ref": "dependency_check.checks[]",
    }


def _failed_adapter_check(target: dict[str, Any], error_type: str) -> dict[str, Any]:
    return {
        "dependency_id": str(target.get("dependency_id") or target.get("name") or target.get("url") or _address(target) or "dependency"),
        "protocol": str(target.get("protocol") or _protocol_from_target(target)),
        "target": target.get("url") or _address(target),
        "success": False,
        "duration_ms": 0.0,
        "dns_duration_ms": 0.0,
        "tcp_connect_duration_ms": 0.0,
        "tls_handshake_duration_ms": 0.0,
        "http_status_code": None,
        "failure_phase": "adapter",
        "error_type": error_type,
        "evidence_ref": "dependency_check.checks[]",
    }


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


def _normalize_targets(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item if isinstance(item, dict) else {"url": str(item)} for item in value]
    if isinstance(value, dict):
        return [value]
    if isinstance(value, str) and value.strip():
        return [{"url": item.strip()} for item in value.split(",") if item.strip()]
    return []


def _collector_invocation(task: CollectorTask) -> dict[str, Any]:
    invocation = task.options.get("collector_invocation")
    if isinstance(invocation, dict):
        return invocation
    return {
        "schema_version": "1.0",
        "scope_source": "task_options",
        "collector_family": "dependency_check",
        "target_config": {"dependency_targets": _normalize_targets(task.options.get("targets") or task.options.get("dependencies"))},
    }


def _address(target: dict[str, Any]) -> str:
    host = str(target.get("host") or "")
    port = target.get("port")
    return f"{host}:{port}" if host and port else host


def _blackbox_probe_target(target: dict[str, Any]) -> str:
    protocol = _protocol_from_target(target)
    if protocol in {"http", "https"} and target.get("url"):
        return str(target["url"])
    return _address(target) or str(target.get("url") or "")


def _protocol_from_target(target: dict[str, Any]) -> str:
    url = str(target.get("url") or "")
    if "://" in url:
        return url.split("://", 1)[0]
    return str(target.get("protocol") or "tcp")


def _default_module(target: dict[str, Any]) -> str:
    protocol = _protocol_from_target(target)
    if protocol in {"http", "https"}:
        return "http_2xx"
    if protocol == "grpc":
        return "grpc"
    return "tcp_connect"


def _is_tls_target(target: dict[str, Any]) -> bool:
    return _protocol_from_target(target) == "https"


def _adapter_source(options: dict[str, Any]) -> str:
    if options.get("blackbox_url") or options.get("blackbox_endpoint"):
        return "blackbox_exporter_http_probe"
    if options.get("metrics_path"):
        return "blackbox_exporter_metrics_file"
    return "blackbox_exporter_metrics_inline"


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
