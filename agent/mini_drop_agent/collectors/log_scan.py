"""Industrial log collector adapter.

This collector does not tail application logs directly. It consumes bounded
JSON/NDJSON output produced by Fluent Bit or OpenTelemetry filelog pipelines,
then normalizes it into Mini-Drop's `log_window_json` evidence contract.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.mini_drop_agent.collectors.base import CollectorResult, CollectorTask


class LogScanCollector:
    """Normalize Fluent Bit / OpenTelemetry log pipeline output."""

    OUTPUT_BASE = "/tmp/mini-drop"
    MAX_RECORDS = 10000
    ERROR_PATTERN = re.compile(
        r"(error|warn|exception|timeout|deadline|refused|reset|unavailable|"
        r"oom|killed|no space|enospc|dns|lookup|redis|payment|failed)",
        re.IGNORECASE,
    )
    TRACE_PATTERN = re.compile(r"\b(?:trace[_-]?id|traceId)[=:]\s*([a-zA-Z0-9._:-]+)", re.IGNORECASE)
    ENDPOINT_PATTERN = re.compile(r"\b(?:GET|POST|PUT|DELETE|PATCH)\s+(/[^\s?]+)")
    DEPENDENCY_PATTERN = re.compile(
        r"\b(redis[-_\w]*|paymentservice|checkoutservice|cartservice|"
        r"productcatalogservice|currencyservice|shippingservice|emailservice)\b",
        re.IGNORECASE,
    )

    def collect(self, task: CollectorTask) -> CollectorResult:
        target_config = task.options.get("target_config")
        if not isinstance(target_config, dict):
            target_config = {}
        source_paths = _normalize_paths(
            task.options.get("source_paths")
            or task.options.get("source_path")
            or task.options.get("adapter_output_paths")
            or task.options.get("log_pipeline_output")
            or target_config.get("source_paths")
            or target_config.get("log_paths")
            or os.getenv("MINI_DROP_LOG_PIPELINE_OUTPUT", "/var/lib/mini-drop/logs/logs.ndjson")
        )
        container_id = str(
            task.options.get("container_id")
            or target_config.get("container_id")
            or ""
        ).strip()
        if container_id:
            source_paths.extend(_docker_log_paths(container_id))
        source_paths = list(dict.fromkeys(source_paths))

        output_dir = os.path.join(self.OUTPUT_BASE, task.id)
        os.makedirs(output_dir, exist_ok=True)
        window_start = _parse_time(task.options.get("window_start")) or (time.time() - task.duration_sec)
        window_end = _parse_time(task.options.get("window_end")) or time.time()
        evidence_window = _evidence_window(task, window_start, window_end)
        max_records = max(1, min(_safe_int(task.options.get("max_records"), self.MAX_RECORDS), self.MAX_RECORDS))

        records = []
        corrupt_paths = []
        readable_paths = []
        source_states = []
        for path in _expand_paths(source_paths):
            parsed, parse_status = _read_json_records_with_status(
                path,
                max_records=max_records - len(records),
            )
            records.extend(parsed)
            if parse_status in {"readable", "empty"}:
                readable_paths.append(str(path))
            if parse_status == "corrupt":
                corrupt_paths.append(str(path))
            source_states.append(parse_status)
            if len(records) >= max_records:
                break

        selected = [
            _normalize_log_record(record)
            for record in records
            if _in_window(_record_timestamp(record), window_start, window_end)
        ]
        matched = [record for record in selected if _is_error_like(record)]
        clusters = _cluster_records(matched)
        observed_times = [item["timestamp"] for item in matched if item.get("timestamp") is not None]
        if corrupt_paths and not readable_paths and not records:
            source_status = "corrupt_input"
        elif records and selected and not matched:
            source_status = "no_error"
        elif records:
            source_status = "readable"
        elif readable_paths:
            source_status = "empty_window"
        else:
            source_status = "source_missing"
        output = {
            "schema_version": "1.0",
            "task_id": task.id,
            "collector_type": "log_scan",
            "collector_family": "log_scan",
            **_cohort_fields(task),
            "adapter": {
                "kind": task.options.get("adapter_kind") or "fluent_bit_or_otel_filelog",
                "source": "industrial_log_pipeline_output",
                "source_paths": source_paths,
                "readable_paths": readable_paths,
                "corrupt_paths": corrupt_paths,
                "source_states": source_states,
                "source_status": source_status,
                "fallback_source": (
                    "docker_json_log"
                    if any(path.startswith("/var/lib/docker/containers/") or
                           path.startswith("/host/var/lib/docker/containers/")
                           for path in source_paths)
                    else None
                ),
            },
            "target_pid": task.target_pid,
            "evidence_window": evidence_window,
            "summary": {
                "total_records_read": len(records),
                "window_records": len(selected),
                "matched_records": len(matched),
                "error_cluster_count": len(clusters),
                "first_seen": min(observed_times) if observed_times else None,
                "last_seen": max(observed_times) if observed_times else None,
                "error_count": sum(1 for item in matched if item["error_type"] in {"error", "exception", "failed"}),
                "warn_count": sum(1 for item in matched if item["error_type"] == "warn"),
                "timeout_count": sum(1 for item in matched if item["error_type"] == "timeout"),
                "dependency_error_count": sum(1 for item in matched if item.get("dependencies")),
                "source_status": source_status,
            },
            "error_clusters": clusters,
            "evidence_index": {
                "trace_ids": _unique([tid for item in matched for tid in item.get("trace_ids", [])])[:50],
                "dependencies": _unique([dep for item in matched for dep in item.get("dependencies", [])])[:50],
                "endpoints": _unique([item.get("endpoint", "") for item in matched if item.get("endpoint")])[:50],
                "error_types": _unique([item.get("error_type", "") for item in matched if item.get("error_type")]),
            },
        }

        output_path = os.path.join(output_dir, "log_window.json")
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(output, fh, ensure_ascii=False, indent=2)
        return CollectorResult(
            ok=source_status not in {"corrupt_input", "source_missing"},
            reason=f"日志适配完成: 来源 {source_status}, 读取 {len(records)} 条, 窗口内 {len(selected)} 条, 错误簇 {len(clusters)} 个",
            artifacts=[{
                "artifact_type": "log_window_json",
                "filename": "log_window.json",
                "local_path": output_path,
                "content_type": "application/json",
                "size_bytes": os.path.getsize(output_path),
                "collector_family": "log_scan",
                "evidence_window": evidence_window,
                **_cohort_fields(task),
                "metadata": output["summary"],
            }],
        )


def _normalize_paths(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _expand_paths(paths: list[str]) -> list[Path]:
    result: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            result.append(path)
        elif path.is_dir():
            result.extend(sorted(item for item in path.glob("*.json") if item.is_file()))
            result.extend(sorted(item for item in path.glob("*.ndjson") if item.is_file()))
            result.extend(sorted(item for item in path.glob("*.jsonl") if item.is_file()))
    return result


def _docker_log_paths(container_id: str) -> list[str]:
    """Use Docker's json-file output as a fallback, never as a second parser."""
    safe_id = re.sub(r"[^a-fA-F0-9_.-]", "", container_id)
    if not safe_id:
        return []
    return [
        f"/var/lib/docker/containers/{safe_id}/{safe_id}-json.log",
        f"/host/var/lib/docker/containers/{safe_id}/{safe_id}-json.log",
    ]


def _read_json_records(path: Path, *, max_records: int) -> list[dict[str, Any]]:
    records, _ = _read_json_records_with_status(path, max_records=max_records)
    return records


def _read_json_records_with_status(path: Path, *, max_records: int) -> tuple[list[dict[str, Any]], str]:
    if max_records <= 0:
        return [], "empty"
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError, UnicodeDecodeError):
        return [], "unreadable"
    if not text.strip():
        return [], "empty"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        records = [item for item in parsed[:max_records] if isinstance(item, dict)]
        return records, "readable" if records else "empty"
    if isinstance(parsed, dict):
        for key in ("records", "logs", "resourceLogs"):
            items = parsed.get(key)
            if isinstance(items, list):
                records = [item for item in items[:max_records] if isinstance(item, dict)]
                return records, "readable" if records else "empty"
        return [parsed], "readable"

    records: list[dict[str, Any]] = []
    invalid_lines = 0
    for line in text.splitlines():
        if len(records) >= max_records:
            break
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            invalid_lines += 1
            continue
        if isinstance(item, dict):
            records.append(item)
    if records:
        return records, "readable"
    return [], "corrupt" if invalid_lines else "empty"


def _normalize_log_record(record: dict[str, Any]) -> dict[str, Any]:
    message = _message(record)
    trace_ids = _trace_ids(record, message)
    endpoint = _field(record, ("endpoint", "http.target", "http.route", "route")) or _first_match(LogScanCollector.ENDPOINT_PATTERN, message)
    dependencies = _dependencies(record, message)
    return {
        "timestamp": _record_timestamp(record),
        "severity": str(_field(record, ("severity", "severity_text", "level", "log.level")) or "").lower(),
        "message": message[:1000],
        "fingerprint": _fingerprint(message),
        "trace_ids": trace_ids,
        "endpoint": endpoint,
        "dependencies": dependencies,
        "error_type": _error_type(record, message),
    }


def _message(record: dict[str, Any]) -> str:
    value = _field(record, ("message", "msg", "log", "body", "event.original"))
    if isinstance(value, dict):
        value = value.get("stringValue") or value.get("value")
    return str(value or "")


def _field(record: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in record:
            return record[name]
    attributes = record.get("attributes")
    if isinstance(attributes, dict):
        for name in names:
            if name in attributes:
                return attributes[name]
    resource = record.get("resource")
    if isinstance(resource, dict):
        for name in names:
            if name in resource:
                return resource[name]
    return None


def _record_timestamp(record: dict[str, Any]) -> float | None:
    value = _field(record, ("timestamp", "@timestamp", "time", "observedTimestamp", "observed_time_unix_nano"))
    return _parse_time(value)


def _parse_time(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 10_000_000_000_000:
            return raw / 1_000_000_000
        if raw > 10_000_000_000:
            return raw / 1000
        return raw
    text = str(value).strip()
    if not text:
        return None
    try:
        return _parse_time(float(text))
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _in_window(timestamp: float | None, start: float, end: float) -> bool:
    return timestamp is None or start <= timestamp <= end


def _is_error_like(record: dict[str, Any]) -> bool:
    severity = record.get("severity", "")
    message = record.get("message", "")
    return severity in {"warn", "warning", "error", "fatal", "critical"} or bool(LogScanCollector.ERROR_PATTERN.search(message))


def _trace_ids(record: dict[str, Any], message: str) -> list[str]:
    values = []
    for key in ("trace_id", "traceId", "trace.id", "traceid"):
        value = _field(record, (key,))
        if value:
            values.append(str(value))
    values.extend(LogScanCollector.TRACE_PATTERN.findall(message))
    return _unique(values)[:10]


def _dependencies(record: dict[str, Any], message: str) -> list[str]:
    values = []
    for key in ("dependency", "dependency_id", "peer.service", "db.system", "net.peer.name"):
        value = _field(record, (key,))
        if value:
            values.append(str(value).lower())
    values.extend(item.lower() for item in LogScanCollector.DEPENDENCY_PATTERN.findall(message))
    return _unique(values)[:10]


def _cluster_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts = Counter(item["fingerprint"] for item in records)
    clusters = []
    for index, (fingerprint, count) in enumerate(counts.most_common(10)):
        samples = [item for item in records if item["fingerprint"] == fingerprint]
        first = samples[0] if samples else {}
        clusters.append({
            "cluster_id": f"log_cluster_{index + 1}",
            "fingerprint": fingerprint,
            "count": count,
            "sample": str(first.get("message", ""))[:300],
            "error_type": first.get("error_type", "unknown"),
            "trace_ids": _unique([tid for item in samples for tid in item.get("trace_ids", [])])[:10],
            "endpoints": _unique([item.get("endpoint", "") for item in samples if item.get("endpoint")])[:10],
            "dependencies": _unique([dep for item in samples for dep in item.get("dependencies", [])])[:10],
            "evidence_ref": f"log_scan.error_clusters[{index}]",
        })
    return clusters


def _fingerprint(line: str) -> str:
    value = line.lower()
    value = re.sub(r"\b[0-9a-f]{8,}\b", "<hex>", value)
    value = re.sub(r"\b\d+\b", "<num>", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:160]


def _error_type(record: dict[str, Any], message: str) -> str:
    severity = str(record.get("severity") or "").lower()
    lower = message.lower()
    if "timeout" in lower or "deadline" in lower:
        return "timeout"
    if "exception" in lower:
        return "exception"
    if severity in {"warn", "warning"} or "warn" in lower:
        return "warn"
    if "refused" in lower or "unavailable" in lower or "reset" in lower:
        return "dependency_error"
    if "oom" in lower or "killed" in lower:
        return "oom"
    if "no space" in lower or "enospc" in lower:
        return "filesystem"
    if "failed" in lower:
        return "failed"
    return "error"


def _first_match(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(1) if match else ""


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(str(item) for item in items if item))


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _cohort_fields(task: CollectorTask) -> dict[str, Any]:
    return {
        "trigger_event_id": task.options.get("trigger_event_id"),
        "evidence_cohort_id": task.options.get("evidence_cohort_id"),
        "collection_mode": task.options.get("collection_mode") or "manual_single",
        "timing_relation": task.options.get("timing_relation") or "unknown",
    }


def _evidence_window(task: CollectorTask, start: float, end: float) -> dict[str, Any]:
    return {
        "start": start,
        "end": end,
        "duration_sec": task.duration_sec,
        **_cohort_fields(task),
    }
