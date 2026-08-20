"""复合 Trace endpoint 采集器。

该采集器把栈采样和已有 OTel/SkyWalking Trace 输出放在同一个证据窗口
内进行关联。它不自研分布式追踪，只读取工业采集链路已经产生的结果。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.mini_drop_agent.collectors.base import CollectorResult, CollectorTask
from agent.mini_drop_agent.collectors.evidence_validity import trace_evidence_state
from agent.mini_drop_agent.collectors.perf import PerfCollector


class TraceEndpointCollector:
    """perf/eBPF 栈采样 + OTel/SkyWalking Trace 关联。"""

    OUTPUT_BASE = "/tmp/mini-drop"
    MAX_RECORDS = 10000

    def __init__(self) -> None:
        self._perf = PerfCollector()

    def collect(self, task: CollectorTask) -> CollectorResult:
        output_dir = _task_output_dir(self.OUTPUT_BASE, task.id)
        config = _target_config(task)
        stack_result = self._collect_stack(task, config, output_dir)
        trace_source = _load_trace_source(task, config)
        profile = _build_profile(task, config, stack_result, trace_source)
        profile["evidence_validity"] = trace_evidence_state(profile)

        profile_path = os.path.join(output_dir, "trace_endpoint_profile.json")
        with open(profile_path, "w", encoding="utf-8") as fh:
            json.dump(profile, fh, ensure_ascii=False, indent=2)

        artifacts = list(stack_result["artifacts"])
        artifacts.append({
            "artifact_type": "trace_endpoint_profile_json",
            "filename": "trace_endpoint_profile.json",
            "local_path": profile_path,
            "content_type": "application/json",
            "size_bytes": os.path.getsize(profile_path),
            "collector_family": "trace_endpoint_profile",
            "evidence_window": profile["evidence_window"],
            "metadata": {"data": profile},
        })
        status = profile["correlation_status"]["status"]
        reason = (
            f"Trace endpoint 复合采集完成: 栈 {profile['stack_source']['status']}, "
            f"Trace {profile['trace_source']['status']}, 关联 {status}"
        )
        # 即使栈采样被权限阻断，也必须上传结构化 profile，保留可审计的失败证据。
        return CollectorResult(
            ok=profile["evidence_validity"]["evidence_status"] in {"valid", "partial", "empty_window"},
            reason=reason,
            artifacts=artifacts,
        )

    def _collect_stack(
        self,
        task: CollectorTask,
        config: dict[str, Any],
        output_dir: str,
    ) -> dict[str, Any]:
        capability_check = _trace_capability_check(task)
        source = str(config.get("stack_source") or "auto").lower()
        if source in {"auto", "ebpf"} and _env_bool("MINI_DROP_EBPF_PROFILE_ENABLED", True):
            ebpf = _collect_ebpf_profile(task, output_dir)
            if ebpf["status"] == "completed":
                return ebpf
            if source == "ebpf":
                return ebpf

        derived = replace(
            task,
            options={
                **task.options,
                "callgraph": task.options.get("callgraph", "fp"),
                "event": task.options.get("event", "cpu-cycles:u"),
            },
        )
        result = self._perf.collect(derived)
        artifacts = list(result.artifacts)
        top_functions = []
        depth = {}
        for artifact in artifacts:
            if artifact.get("artifact_type") == "top_json":
                top_functions = _read_json_path(artifact.get("local_path")) or []
            elif artifact.get("artifact_type") == "depth_evidence_json":
                depth = _read_json_path(artifact.get("local_path")) or {}
        if result.ok:
            return {
                "source": "perf",
                "status": "completed",
                "blocked_reason": "",
                "blocked_details": {},
                "capability_check": capability_check,
                "artifacts": artifacts,
                "top_functions": top_functions if isinstance(top_functions, list) else [],
                "depth": depth if isinstance(depth, dict) else {},
            }
        return {
            "source": "perf",
            "status": "blocked",
            "blocked_reason": result.reason,
            "blocked_details": {
                **_stack_blocked_details(result.reason),
                "capability_check": capability_check,
            },
            "capability_check": capability_check,
            "artifacts": artifacts,
            "top_functions": [],
            "depth": {},
        }


def _target_config(task: CollectorTask) -> dict[str, Any]:
    value = task.options.get("target_config")
    return value if isinstance(value, dict) else {}


def _load_trace_source(task: CollectorTask, config: dict[str, Any]) -> dict[str, Any]:
    paths = _normalize_paths(
        config.get("trace_paths")
        or task.options.get("trace_paths")
        or os.getenv("MINI_DROP_TRACE_PATHS", "/var/lib/mini-drop/traces")
    )
    records: list[dict[str, Any]] = []
    readable_paths: list[str] = []
    existing_sources: list[str] = []
    for path in _expand_paths(paths):
        readable_paths.append(str(path))
        records.extend(_read_trace_records(path, max_records=TraceEndpointCollector.MAX_RECORDS - len(records)))
        if len(records) >= TraceEndpointCollector.MAX_RECORDS:
            break
    for raw_path in paths:
        candidate = Path(raw_path)
        if candidate.exists():
            existing_sources.append(str(candidate))
    normalized = [_normalize_trace_record(item) for item in records]
    normalized = [
        item for item in normalized
        if (item.get("trace_id") or item.get("endpoint"))
        and _trace_in_window(item, task)
    ]
    if normalized:
        status = "completed"
    elif readable_paths or existing_sources:
        status = "empty_window"
    else:
        status = "unavailable"
    return {
        "kind": str(config.get("trace_source") or "auto"),
        "status": status,
        "paths": paths,
        "readable_paths": readable_paths,
        "existing_sources": existing_sources,
        "records_read": len(records),
        "records_in_window": len(normalized),
        "records": normalized,
        "blocked_reason": "" if status != "unavailable" else "trace_source_missing",
    }


def _build_profile(
    task: CollectorTask,
    config: dict[str, Any],
    stack: dict[str, Any],
    trace: dict[str, Any],
) -> dict[str, Any]:
    depth = stack.get("depth") if isinstance(stack.get("depth"), dict) else {}
    stack_samples = depth.get("stack_samples") if isinstance(depth.get("stack_samples"), list) else []
    top_functions = stack.get("top_functions") if isinstance(stack.get("top_functions"), list) else []
    if not top_functions:
        top_functions = _top_from_stack_samples(stack_samples)
    window = _window(task)
    bindings, hotspots = _correlate(
        top_functions=top_functions,
        stack_samples=stack_samples,
        trace_records=trace.get("records", []),
        config=config,
        window=window,
    )
    max_level = "function" if top_functions else "process"
    if hotspots and any(item.get("call_path") for item in hotspots):
        max_level = "call_path"
    elif bindings:
        max_level = "endpoint"
    status = "blocked" if stack.get("status") == "blocked" else "unmatched"
    if bindings or hotspots:
        status = "completed" if max_level == "call_path" else "partial"
    elif top_functions and trace.get("status") in {"unavailable", "empty_window"} and stack.get("status") == "completed":
        status = "partial"
    elif stack.get("status") != "blocked" and not top_functions and trace.get("status") in {"unavailable", "empty_window"}:
        status = "empty_window"
    return {
        "schema_version": "1.0",
        "task_id": task.id,
        "collector_type": "trace_endpoint_profile",
        "collector_family": "trace_endpoint_profile",
        "target": {
            "pid": task.target_pid,
            "service_id": str(config.get("service_id") or ""),
            "instance_id": str(config.get("instance_id") or ""),
            "host_id": str(config.get("host_id") or ""),
            "endpoint": str(config.get("endpoint") or ""),
        },
        "evidence_window": window,
        "stack_source": {
            "kind": stack.get("source") or "unknown",
            "status": stack.get("status") or "unavailable",
            "blocked_reason": stack.get("blocked_reason") or "",
            "blocked_details": stack.get("blocked_details") or {},
            "capability_check": stack.get("capability_check") or {},
            "artifacts": [
                item.get("filename") for item in stack.get("artifacts", [])
                if item.get("filename")
            ],
        },
        "trace_source": {
            key: value for key, value in trace.items()
            if key != "records"
        },
        "capability_check": stack.get("capability_check") or {},
        "top_functions": [
            {
                "name": str(item.get("name") or item.get("function") or ""),
                "samples": _safe_int(item.get("samples") or item.get("sample_count")),
                "percent": _safe_float(item.get("percent")),
                "evidence_ref": f"trace_endpoint_profile.top_functions[{index}]",
            }
            for index, item in enumerate(top_functions[:10])
            if str(item.get("name") or item.get("function") or "")
        ],
        "endpoint_bindings": bindings,
        "call_path_hotspots": hotspots,
        "correlation_status": {
            "status": status,
            "max_supported_level": max_level,
            "blocked_reason": stack.get("blocked_reason") or trace.get("blocked_reason") or "",
            "blocked_details": stack.get("blocked_details") or {},
            "warnings": _warnings(stack, trace, bindings),
            "missing": _missing(stack, trace, bindings),
        },
        "trigger_event_id": task.options.get("trigger_event_id"),
        "evidence_cohort_id": task.options.get("evidence_cohort_id"),
        "collection_mode": task.options.get("collection_mode") or "manual_single",
        "timing_relation": task.options.get("timing_relation") or "unknown",
    }


def _correlate(
    *,
    top_functions: list[dict[str, Any]],
    stack_samples: list[dict[str, Any]],
    trace_records: list[dict[str, Any]],
    config: dict[str, Any],
    window: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    bindings: list[dict[str, Any]] = []
    hotspots: list[dict[str, Any]] = []
    sample_items = stack_samples or top_functions
    for index, sample in enumerate(sample_items[:10]):
        function = str(sample.get("hot_frame") or sample.get("name") or sample.get("function") or "")
        if not function:
            continue
        trace_id = str(sample.get("trace_id") or "")
        candidates = sorted(
            (
                (_correlation_score(item, sample, config, trace_id, window), item)
                for item in trace_records
            ),
            key=lambda pair: (-pair[0][0], -pair[0][1], pair[1].get("trace_id", ""), pair[1].get("span_id", "")),
        )
        if not candidates or candidates[0][0][0] <= 0:
            continue
        score, span = candidates[0]
        # 仅 service + 时间重叠只能形成 endpoint 候选，不能把完整调用链当成已确认事实。
        path = (
            _call_path(span, trace_records)
            if score[2] in {"explicit_trace_context", "pid_instance_time_overlap"}
            else []
        )
        binding_ref = f"trace_endpoint_profile.endpoint_bindings[{len(bindings)}]"
        binding = {
            "endpoint": span.get("endpoint") or str(config.get("endpoint") or ""),
            "service_id": span.get("service_id") or str(config.get("service_id") or ""),
            "instance_id": span.get("instance_id") or str(config.get("instance_id") or ""),
            "trace_ids": [span.get("trace_id")] if span.get("trace_id") else [],
            "span_ids": [span.get("span_id")] if span.get("span_id") else [],
            "correlation_method": score[2],
            "confidence": score[0],
            "evidence_ref": binding_ref,
        }
        if binding["endpoint"] or binding["trace_ids"]:
            bindings.append(binding)
        hotspots.append({
            "function": function,
            "samples": _safe_int(sample.get("sample_count") or sample.get("samples")),
            "percent": _safe_float(sample.get("percent")),
            "endpoint": binding["endpoint"],
            "service_id": binding["service_id"],
            "instance_id": binding["instance_id"],
            "trace_ids": binding["trace_ids"],
            "call_path": path,
            "correlation_method": score[2],
            "confidence": score[0],
            "evidence_ref": f"trace_endpoint_profile.call_path_hotspots[{index}]",
        })
    return _dedupe_bindings(bindings), hotspots


def _correlation_score(
    span: dict[str, Any],
    sample: dict[str, Any],
    config: dict[str, Any],
    trace_id: str,
    window: dict[str, Any],
) -> tuple[float, float, str]:
    if not _span_overlaps_window(span, window.get("start"), window.get("end")):
        return 0.0, 0.0, "outside_window"
    if trace_id and trace_id == span.get("trace_id"):
        return 0.98, _safe_float(span.get("duration_ms")), "explicit_trace_context"
    target_pid = str(config.get("pid") or "")
    span_pid = str(span.get("pid") or "")
    if target_pid and target_pid == span_pid:
        return 0.86, _safe_float(span.get("duration_ms")), "pid_instance_time_overlap"
    target_instance = str(config.get("instance_id") or "")
    if target_instance and target_instance == span.get("instance_id"):
        return 0.76, _safe_float(span.get("duration_ms")), "pid_instance_time_overlap"
    target_service = str(config.get("service_id") or "")
    if target_service and target_service == span.get("service_id"):
        return 0.62, _safe_float(span.get("duration_ms")), "service_time_overlap"
    return 0.0, 0.0, "unmatched"


def _span_overlaps_window(span: dict[str, Any], start: Any, end: Any) -> bool:
    start_value = _parse_time(start)
    end_value = _parse_time(end)
    span_start = span.get("start")
    span_end = span.get("end")
    if start_value is None or end_value is None or span_start is None:
        return True
    return (span_end or span_start) >= start_value and span_start <= end_value


def _trace_in_window(record: dict[str, Any], task: CollectorTask) -> bool:
    start = _parse_time(task.options.get("window_start"))
    end = _parse_time(task.options.get("window_end"))
    if start is None or end is None:
        end = time.time()
        start = end - task.duration_sec
    return _span_overlaps_window(record, start, end)


def _call_path(span: dict[str, Any], records: list[dict[str, Any]]) -> list[str]:
    explicit = span.get("call_path")
    if isinstance(explicit, list) and explicit:
        return [str(item) for item in explicit]
    by_id = {item.get("span_id"): item for item in records if item.get("span_id")}
    path: list[str] = []
    current = span
    seen: set[str] = set()
    while current and current.get("span_id") not in seen:
        span_id = current.get("span_id")
        if span_id:
            seen.add(span_id)
        service = str(current.get("service_id") or "")
        if service and service not in path:
            path.append(service)
        parent = current.get("parent_span_id")
        current = by_id.get(parent)
    path.reverse()
    peer = str(span.get("peer_service") or "")
    if peer and peer not in path:
        path.append(peer)
    return path


def _normalize_trace_record(record: dict[str, Any]) -> dict[str, Any]:
    attrs = record.get("attributes") if isinstance(record.get("attributes"), dict) else {}
    resource = record.get("resource") if isinstance(record.get("resource"), dict) else {}
    resource_attrs = resource.get("attributes") if isinstance(resource.get("attributes"), dict) else {}
    def field(*names: str) -> Any:
        for name in names:
            if name in record:
                return record[name]
            if name in attrs:
                return attrs[name]
            if name in resource_attrs:
                return resource_attrs[name]
        return None
    start = _parse_time(field("start_time", "startTime", "startTimestamp", "timestamp"))
    end = _parse_time(field("end_time", "endTime", "endTimestamp"))
    if end is None and start is not None:
        duration = _safe_float(field("duration_ms", "duration", "latency"))
        end = start + duration / 1000 if duration else start
    return {
        "trace_id": str(field("trace_id", "traceId", "traceID") or ""),
        "span_id": str(field("span_id", "spanId", "spanID", "segmentId") or ""),
        "parent_span_id": str(field("parent_span_id", "parentSpanId", "parentSpanID") or ""),
        "service_id": str(field("service", "serviceName", "serviceCode", "service.name") or ""),
        "instance_id": str(field("instance", "instanceName", "service.instance.id") or ""),
        "pid": str(field("pid", "process.pid", "process_pid") or ""),
        "endpoint": str(field("endpoint", "endpointName", "operationName", "name", "http.route", "http.target") or ""),
        "peer_service": str(field("peer.service", "peer", "net.peer.name") or ""),
        "start": start,
        "end": end,
        "duration_ms": _safe_float(field("duration_ms", "duration", "latency")),
        "call_path": field("call_path"),
    }


def _collect_ebpf_profile(task: CollectorTask, output_dir: str) -> dict[str, Any]:
    bpftrace = shutil.which("bpftrace")
    capability_check = _trace_capability_check(task)
    if not bpftrace:
        return {
            "source": "ebpf",
            "status": "unavailable",
            "blocked_reason": "bpftrace_not_installed",
            "blocked_details": {
                "missing_tools": ["bpftrace"],
                "repair_action": "安装 bpftrace，或回退到 perf 并确保 perf_event_paranoid 允许采样。",
            },
            "capability_check": capability_check,
            "artifacts": [],
            "top_functions": [],
            "depth": {},
        }
    raw_path = os.path.join(output_dir, "ebpf_profile.txt")
    script = f'profile:hz:{max(1, min(task.sample_rate, 999))} /pid == {task.target_pid}/ {{ @[ustack] = count(); }}'
    try:
        proc = subprocess.Popen(
            [bpftrace, "-e", script, "-o", raw_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=hasattr(os, "setsid"),
        )
        try:
            proc.wait(timeout=task.duration_sec)
        except subprocess.TimeoutExpired:
            proc.send_signal(signal.SIGINT)
            proc.communicate(timeout=10)
        if proc.returncode not in {0, -2, -15, 255}:
            stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            return {
                "source": "ebpf",
                "status": "blocked",
                "blocked_reason": f"bpftrace 执行失败: {stderr[:240]}",
                "blocked_details": _stack_blocked_details(stderr),
                "capability_check": capability_check,
                "artifacts": [],
                "top_functions": [],
                "depth": {},
            }
    except OSError as exc:
        return {
            "source": "ebpf",
            "status": "blocked",
            "blocked_reason": f"ebpf_permission_or_runtime_error: {exc}",
            "blocked_details": _stack_blocked_details(str(exc)),
            "capability_check": capability_check,
            "artifacts": [],
            "top_functions": [],
            "depth": {},
        }
    if not os.path.isfile(raw_path):
        return {
            "source": "ebpf",
            "status": "blocked",
            "blocked_reason": "ebpf_profile_output_missing",
            "blocked_details": {
                "repair_action": "检查 bpftrace 输出目录、容器挂载和 BPF/PERFMON capability。",
            },
            "capability_check": capability_check,
            "artifacts": [],
            "top_functions": [],
            "depth": {},
        }
    top = _parse_ebpf_profile(raw_path)
    return {
        "source": "ebpf",
        "status": "completed",
        "blocked_reason": "",
        "blocked_details": {},
        "capability_check": capability_check,
        "artifacts": [{
            "artifact_type": "ebpf_profile_raw",
            "filename": "ebpf_profile.txt",
            "local_path": raw_path,
            "content_type": "text/plain",
            "size_bytes": os.path.getsize(raw_path),
        }],
        "top_functions": top,
        "depth": {},
    }


def _parse_ebpf_profile(path: str) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    current: list[str] = []
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("@"):
            current = []
            continue
        if stripped.startswith("}") or not stripped:
            continue
        match = re.match(r"(.+?)(?::\s*(\d+))?$", stripped)
        if not match:
            continue
        name, count = match.group(1).strip(), match.group(2)
        if count:
            for frame in current:
                counts[frame] = counts.get(frame, 0) + int(count)
            current = []
        elif not name.startswith("["):
            current.append(name)
    total = sum(counts.values())
    return [
        {
            "name": name,
            "samples": count,
            "percent": round(count / total * 100, 2) if total else 0.0,
        }
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:10]
    ]


def _read_trace_records(path: Path, *, max_records: int) -> list[dict[str, Any]]:
    if max_records <= 0:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
    if value is not None:
        return _flatten_trace_payload(value)[:max_records]
    records = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        records.extend(_flatten_trace_payload(item))
        if len(records) >= max_records:
            break
    return records[:max_records]


def _flatten_trace_payload(value: Any) -> list[dict[str, Any]]:
    return _flatten_trace_payload_with_context(value, resource_attrs={})


def _flatten_trace_payload_with_context(value: Any, *, resource_attrs: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_flatten_trace_payload_with_context(item, resource_attrs=resource_attrs))
        return result
    if not isinstance(value, dict):
        return []
    for key in ("spans", "records", "traceSpans"):
        if isinstance(value.get(key), list):
            return _flatten_trace_payload_with_context(value[key], resource_attrs=resource_attrs)
    resource_spans = value.get("resourceSpans")
    if isinstance(resource_spans, list):
        result = []
        for resource in resource_spans:
            if not isinstance(resource, dict):
                continue
            attrs = _resource_attributes(resource.get("resource"))
            for scope in resource.get("scopeSpans", []) if isinstance(resource.get("scopeSpans"), list) else []:
                result.extend(_flatten_trace_payload_with_context(
                    scope.get("spans", []),
                    resource_attrs=attrs,
                ))
        return result
    if resource_attrs and "resource" not in value:
        return [{**value, "resource": {"attributes": resource_attrs}}]
    return [value]


def _resource_attributes(resource: Any) -> dict[str, Any]:
    if not isinstance(resource, dict):
        return {}
    attrs = resource.get("attributes")
    if isinstance(attrs, dict):
        return attrs
    if not isinstance(attrs, list):
        return {}
    result: dict[str, Any] = {}
    for item in attrs:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        if key:
            result[key] = _otel_any_value(item.get("value"))
    return result


def _otel_any_value(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            return value[key]
    if "arrayValue" in value and isinstance(value["arrayValue"], dict):
        return [_otel_any_value(item) for item in value["arrayValue"].get("values") or []]
    return value


def _expand_paths(paths: list[str]) -> list[Path]:
    result: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            result.append(path)
        elif path.is_dir():
            result.extend(sorted(item for item in path.glob("*.json*") if item.is_file()))
    return result


def _normalize_paths(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _top_from_stack_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": item.get("hot_frame"),
            "samples": item.get("sample_count"),
            "percent": item.get("percent"),
            "trace_id": item.get("trace_id"),
        }
        for item in samples[:10]
        if item.get("hot_frame")
    ]


def _read_json_path(path: Any) -> Any:
    if not path:
        return None
    try:
        return json.loads(Path(str(path)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


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
    try:
        return float(text)
    except ValueError:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None


def _window(task: CollectorTask) -> dict[str, Any]:
    end = _parse_time(task.options.get("window_end")) or time.time()
    start = _parse_time(task.options.get("window_start")) or end - task.duration_sec
    return {
        "start": start,
        "end": end,
        "duration_sec": task.duration_sec,
        "trigger_event_id": task.options.get("trigger_event_id"),
        "evidence_cohort_id": task.options.get("evidence_cohort_id"),
        "collection_mode": task.options.get("collection_mode") or "manual_single",
        "timing_relation": task.options.get("timing_relation") or "unknown",
    }


def _warnings(stack: dict[str, Any], trace: dict[str, Any], bindings: list[dict[str, Any]]) -> list[str]:
    warnings = []
    if stack.get("status") != "completed":
        warnings.append(str(stack.get("blocked_reason") or "stack_source_unavailable"))
    if trace.get("status") != "completed":
        warnings.append(str(trace.get("blocked_reason") or "trace_source_unavailable"))
    if trace.get("status") == "completed" and not bindings:
        warnings.append("trace_source_available_but_unmatched")
    return list(dict.fromkeys(warnings))


def _missing(stack: dict[str, Any], trace: dict[str, Any], bindings: list[dict[str, Any]]) -> list[str]:
    missing = []
    if stack.get("status") != "completed":
        missing.append("stack_profile")
    if trace.get("status") != "completed":
        missing.append("trace_source")
    if trace.get("status") == "completed" and not bindings:
        missing.append("trace_context_match")
    return missing


def _dedupe_bindings(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for item in items:
        key = (item.get("endpoint"), tuple(item.get("trace_ids") or []), item.get("service_id"))
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _stack_blocked_details(reason: str) -> dict[str, Any]:
    paranoid = None
    try:
        paranoid = int(Path("/proc/sys/kernel/perf_event_paranoid").read_text().strip())
    except (OSError, ValueError):
        pass
    lower = reason.lower()
    missing_capabilities = [
        capability
        for capability, tokens in {
            "PERFMON": ("perf_event_open", "perf_event_paranoid", "permission denied", "perf_event"),
            "SYS_PTRACE": ("ptrace", "operation not permitted"),
            "BPF": ("bpf", "bpftrace"),
        }.items()
        if any(token in lower for token in tokens)
    ]
    return {
        "perf_event_paranoid": paranoid,
        "missing_capabilities": list(dict.fromkeys(missing_capabilities)),
        "repair_action": (
            "在 Worker Agent 容器启用 privileged、pid: host、PERFMON、SYS_PTRACE、SYS_ADMIN、BPF，"
            "并将 kernel.perf_event_paranoid 调整到允许采样的值后重启 Agent。"
            if missing_capabilities or paranoid is not None
            else "检查 perf/bpftrace 安装、容器 capability、内核配置和目标进程权限。"
        ),
    }


def _trace_capability_check(task: CollectorTask) -> dict[str, Any]:
    paranoid = None
    try:
        paranoid = int(Path("/proc/sys/kernel/perf_event_paranoid").read_text().strip())
    except (OSError, ValueError):
        pass
    cap_eff = ""
    cap_bnd = ""
    seccomp = None
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("CapEff:"):
                cap_eff = line.split(":", 1)[1].strip()
            elif line.startswith("CapBnd:"):
                cap_bnd = line.split(":", 1)[1].strip()
            elif line.startswith("Seccomp:"):
                seccomp = line.split(":", 1)[1].strip()
    except OSError:
        pass
    capability_bits = {
        "SYS_PTRACE": 19,
        "SYS_ADMIN": 21,
        "PERFMON": 38,
        "BPF": 39,
    }
    missing_capabilities = []
    if cap_eff:
        try:
            effective = int(cap_eff, 16)
            missing_capabilities = [
                name for name, bit in capability_bits.items()
                if not (effective & (1 << bit))
            ]
        except ValueError:
            missing_capabilities = list(capability_bits)
    return {
        "tools": {
            "bpftrace": bool(shutil.which("bpftrace")),
            "perf": bool(shutil.which("perf")),
        },
        "perf_event_paranoid": paranoid,
        "cap_eff": cap_eff,
        "cap_bnd": cap_bnd,
        "missing_capabilities": missing_capabilities,
        "seccomp": seccomp,
        "pid_namespace": os.path.realpath("/proc/1/ns/pid") if os.path.exists("/proc/1/ns/pid") else None,
        "target_pid": task.target_pid,
        "target_present": os.path.isdir(f"/proc/{task.target_pid}"),
        "tracepoints": {
            "sched_switch": _tracepoint_exists("sched", "sched_switch"),
            "sched_wakeup": _tracepoint_exists("sched", "sched_wakeup"),
        },
        "missing_tools": [
            name for name, present in {
                "bpftrace": bool(shutil.which("bpftrace")),
                "perf": bool(shutil.which("perf")),
            }.items() if not present
        ],
        "repair_action": (
            "检查 bpftrace/perf 安装、Worker 的 privileged/pid:host、PERFMON、"
            "SYS_PTRACE、BPF capability 以及 perf_event_paranoid。"
        ),
    }


def _tracepoint_exists(group: str, event: str) -> bool:
    roots = (
        "/sys/kernel/tracing/events",
        "/sys/kernel/debug/tracing/events",
    )
    return any(os.path.exists(os.path.join(root, group, event)) for root in roots)


def _task_output_dir(base: str, task_id: str) -> str:
    requested = os.path.join(os.getenv("MINI_DROP_OUTPUT_BASE", base), task_id)
    try:
        os.makedirs(requested, exist_ok=True)
        return requested
    except OSError:
        fallback = os.path.join(tempfile.gettempdir(), "mini-drop", task_id)
        os.makedirs(fallback, exist_ok=True)
        return fallback
