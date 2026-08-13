"""Collector artifact to compact evidence structuring.

This layer is intentionally non-AI: it normalizes artifact outputs into
referenceable summaries, but it does not decide root cause.
"""

from __future__ import annotations

import html
import re
from typing import Any, Literal

from pydantic import BaseModel, Field


TOP_LIMIT = 10
HOTSPOT_LIMIT = 10
CollectionMode = Literal[
    "manual_single",
    "manual_group",
    "triggered_group",
    "rolling_snapshot",
    "delayed_followup",
    "baseline_window",
]
TimingRelation = Literal[
    "same_window",
    "pre_trigger_window",
    "post_trigger_window",
    "delayed_followup",
    "stale_window",
    "unknown",
]


class EvidenceWindowMetadata(BaseModel):
    trigger_event_id: str | None = None
    evidence_cohort_id: str | None = None
    collection_mode: CollectionMode = "manual_single"
    window_start: str | None = None
    window_end: str | None = None
    trigger_observed_at: str | None = None
    timing_relation: TimingRelation = "unknown"


class StructuredEvidence(BaseModel):
    version: int = 1
    task_id: str
    trigger_event_id: str | None = None
    evidence_cohort_id: str | None = None
    collection_mode: CollectionMode = "manual_single"
    window_start: str | None = None
    window_end: str | None = None
    trigger_observed_at: str | None = None
    timing_relation: TimingRelation = "unknown"
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)
    top_functions: list[dict[str, Any]] = Field(default_factory=list)
    stack_summary: dict[str, Any] = Field(default_factory=dict)
    call_path_hotspots: list[dict[str, Any]] = Field(default_factory=list)
    evidence_index: dict[str, Any] = Field(default_factory=dict)
    confidence_inputs: dict[str, Any] = Field(default_factory=dict)
    sys_metrics: dict[str, Any] | None = None
    ebpf_metrics: dict[str, Any] | None = None
    memory_json: dict[str, Any] | None = None
    off_cpu_wait_json: dict[str, Any] | None = None
    log_window_json: dict[str, Any] | None = None
    dependency_check_json: dict[str, Any] | None = None
    redis_check_json: dict[str, Any] | None = None
    trace_endpoint_profile_json: dict[str, Any] | None = None


def structure_artifact_evidence(
    *,
    task_id: str,
    artifacts: list[dict[str, Any]],
    artifact_values: dict[str, Any] | None = None,
    evidence_window: dict[str, Any] | EvidenceWindowMetadata | None = None,
) -> StructuredEvidence:
    """Convert raw and semi-raw collector artifacts into compact evidence."""
    values = artifact_values or {}
    window = _normalize_evidence_window(evidence_window or values.get("evidence_window") or _artifact_value_window(values))
    window_json = window.model_dump(mode="json")
    artifact_refs = _with_window_metadata(_build_artifact_refs(task_id, artifacts), window_json)
    top_functions = _normalize_top_functions(values.get("top_json"))
    if not top_functions:
        top_functions = _top_from_off_cpu_wait(values.get("off_cpu_wait_json"))
    if not top_functions:
        top_functions = _top_from_flamegraph_tree(values.get("flamegraph_json"))
    if not top_functions:
        top_functions = _top_from_flamegraph_svg(values.get("flamegraph_svg"))
    top_functions = _with_window_metadata(top_functions, window_json)

    depth = values.get("depth_evidence_json") if isinstance(values.get("depth_evidence_json"), dict) else {}
    stack_summary = {
        **_build_stack_summary(
            depth,
            top_functions,
            artifact_refs,
            off_cpu_wait=values.get("off_cpu_wait_json"),
        ),
        "evidence_window": window_json,
    }
    call_path_hotspots = _with_window_metadata(_build_call_path_hotspots(depth, top_functions), window_json)
    trace_profile = values.get("trace_endpoint_profile_json")
    if isinstance(trace_profile, dict):
        profile_hotspots = _trace_profile_hotspots(trace_profile)
        if profile_hotspots:
            call_path_hotspots = _with_window_metadata(profile_hotspots, window_json)
    confidence_inputs = _build_confidence_inputs(
        top_functions=top_functions,
        stack_summary=stack_summary,
        call_path_hotspots=call_path_hotspots,
        artifact_refs=artifact_refs,
        sys_metrics=values.get("sys_metrics"),
        ebpf_metrics=values.get("ebpf_metrics"),
        off_cpu_wait=values.get("off_cpu_wait_json"),
        log_window=values.get("log_window_json"),
        dependency_check=values.get("dependency_check_json"),
        redis_check=values.get("redis_check_json"),
        trace_profile=trace_profile,
    )
    confidence_inputs["evidence_window"] = window_json
    evidence_index = _build_evidence_index(
        artifact_refs=artifact_refs,
        depth=depth,
        stack_summary=stack_summary,
        call_path_hotspots=call_path_hotspots,
        confidence_inputs=confidence_inputs,
        off_cpu_wait=values.get("off_cpu_wait_json"),
        log_window=values.get("log_window_json"),
        dependency_check=values.get("dependency_check_json"),
        redis_check=values.get("redis_check_json"),
        trace_profile=trace_profile,
    )
    evidence_index["evidence_window"] = window_json
    return StructuredEvidence(
        task_id=task_id,
        trigger_event_id=window.trigger_event_id,
        evidence_cohort_id=window.evidence_cohort_id,
        collection_mode=window.collection_mode,
        window_start=window.window_start,
        window_end=window.window_end,
        trigger_observed_at=window.trigger_observed_at,
        timing_relation=window.timing_relation,
        artifact_refs=artifact_refs,
        top_functions=top_functions,
        stack_summary=stack_summary,
        call_path_hotspots=call_path_hotspots,
        evidence_index=evidence_index,
        confidence_inputs=confidence_inputs,
        sys_metrics=values.get("sys_metrics") if isinstance(values.get("sys_metrics"), dict) else None,
        ebpf_metrics=values.get("ebpf_metrics") if isinstance(values.get("ebpf_metrics"), dict) else None,
        memory_json=values.get("memory_json") if isinstance(values.get("memory_json"), dict) else None,
        off_cpu_wait_json=values.get("off_cpu_wait_json") if isinstance(values.get("off_cpu_wait_json"), dict) else None,
        log_window_json=values.get("log_window_json") if isinstance(values.get("log_window_json"), dict) else None,
        dependency_check_json=values.get("dependency_check_json") if isinstance(values.get("dependency_check_json"), dict) else None,
        redis_check_json=values.get("redis_check_json") if isinstance(values.get("redis_check_json"), dict) else None,
        trace_endpoint_profile_json=trace_profile if isinstance(trace_profile, dict) else None,
    )


def rca_inputs_from_structured(structured: StructuredEvidence) -> dict[str, Any]:
    """Map structured evidence back to existing RCA input fields."""
    return {
        "top_functions": structured.top_functions,
        "sys_metrics": structured.sys_metrics,
        "ebpf_metrics": structured.ebpf_metrics,
        "off_cpu_wait_json": structured.off_cpu_wait_json,
        "evidence_index": structured.evidence_index,
    }


def _normalize_evidence_window(value: Any) -> EvidenceWindowMetadata:
    if isinstance(value, EvidenceWindowMetadata):
        return value
    if isinstance(value, dict):
        return EvidenceWindowMetadata.model_validate(_normalize_window_keys(value))
    return EvidenceWindowMetadata()


def _artifact_value_window(values: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "log_window_json",
        "dependency_check_json",
        "redis_check_json",
        "off_cpu_wait_json",
        "depth_evidence_json",
        "continuous_summary",
    ):
        value = values.get(key)
        if isinstance(value, dict) and isinstance(value.get("evidence_window"), dict):
            return value["evidence_window"]
    return {}


def _normalize_window_keys(value: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(value)
    if "window_start" not in normalized and "start" in normalized:
        normalized["window_start"] = str(normalized["start"]) if normalized["start"] is not None else None
    if "window_end" not in normalized and "end" in normalized:
        normalized["window_end"] = str(normalized["end"]) if normalized["end"] is not None else None
    return normalized


def _with_window_metadata(items: list[dict[str, Any]], window: dict[str, Any]) -> list[dict[str, Any]]:
    return [{**item, "evidence_window": window} for item in items]


def _build_artifact_refs(task_id: str, artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for artifact in sorted(artifacts, key=lambda item: (str(item.get("artifact_type", "")), str(item.get("filename", "")))):
        artifact_type = str(artifact.get("artifact_type") or "unknown")
        refs.append({
            "artifact_type": artifact_type,
            "filename": str(artifact.get("filename") or ""),
            "content_type": str(artifact.get("content_type") or ""),
            "size_bytes": int(artifact.get("size_bytes") or 0),
            "object_key": str(artifact.get("object_key") or ""),
            "local_path": str(artifact.get("local_path") or ""),
            "raw_payload_policy": str(artifact.get("raw_payload_policy") or "references_only"),
            "evidence_ref": f"task:{task_id}:artifact:{artifact_type}",
        })
    return refs


def _normalize_top_functions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("function") or item.get("symbol") or "").strip()
        if not name:
            continue
        samples = _safe_int(item.get("samples") or item.get("sample_count") or item.get("value"))
        percent = _safe_float(item.get("percent"))
        items.append({
            "name": name,
            "samples": samples,
            "percent": round(percent, 2),
        })
    items.sort(key=lambda item: (-float(item.get("percent") or 0.0), -int(item.get("samples") or 0), item["name"]))
    result = []
    for index, item in enumerate(items[:TOP_LIMIT]):
        result.append({**item, "evidence_ref": f"structured_evidence.top_functions[{index}]"})
    return result


def _top_from_off_cpu_wait(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    stacks = value.get("top_wait_stacks")
    if not isinstance(stacks, list):
        return []
    items: list[dict[str, Any]] = []
    for item in stacks:
        if not isinstance(item, dict):
            continue
        stack = item.get("stack")
        name = str(item.get("top_frame") or "").strip()
        if not name and isinstance(stack, list) and stack:
            name = str(stack[0]).strip()
        if not name:
            continue
        items.append({
            "name": name,
            "samples": _safe_int(item.get("samples")),
            "percent": round(_safe_float(item.get("percent")), 2),
            "wait_reason": str(item.get("wait_reason") or ""),
            "source": "off_cpu_wait",
        })
    items.sort(key=lambda item: (-float(item.get("percent") or 0.0), -int(item.get("samples") or 0), item["name"]))
    return [
        {
            **item,
            "evidence_ref": f"structured_evidence.top_functions[{index}]",
        }
        for index, item in enumerate(items[:TOP_LIMIT])
    ]


def _top_from_flamegraph_tree(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    total = _safe_float(value.get("value"))
    counter: dict[str, int] = {}

    def walk(node: dict[str, Any], is_root: bool = False) -> None:
        name = str(node.get("name") or "").strip()
        samples = _safe_int(node.get("value"))
        if name and not is_root:
            counter[name] = max(counter.get(name, 0), samples)
        for child in node.get("children", []) if isinstance(node.get("children"), list) else []:
            if isinstance(child, dict):
                walk(child)

    walk(value, is_root=True)
    entries = sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:TOP_LIMIT]
    return [
        {
            "name": name,
            "samples": samples,
            "percent": round((samples / total * 100.0), 2) if total else 0.0,
            "evidence_ref": f"structured_evidence.top_functions[{index}]",
        }
        for index, (name, samples) in enumerate(entries)
    ]


def _top_from_flamegraph_svg(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, str) or "<title" not in value:
        return []
    counter: dict[str, dict[str, float | int | str]] = {}
    for index, raw_title in enumerate(re.findall(r"<title[^>]*>(.*?)</title>", value, flags=re.IGNORECASE | re.DOTALL)):
        title = html.unescape(re.sub(r"<[^>]+>", "", raw_title)).strip()
        if not title:
            continue
        name = _function_name_from_svg_title(title)
        if not name or name.lower() in {"all", "root"}:
            continue
        percent = _percent_from_text(title)
        samples = _samples_from_text(title)
        if percent <= 0.0 and samples <= 0:
            continue
        current = counter.get(name)
        if current is None or percent > float(current["percent"]) or samples > int(current["samples"]):
            counter[name] = {
                "name": name,
                "samples": samples,
                "percent": round(percent, 2),
                "order": index,
            }
    entries = sorted(
        counter.values(),
        key=lambda item: (-float(item["percent"]), -int(item["samples"]), int(item["order"]), str(item["name"])),
    )[:TOP_LIMIT]
    return [
        {
            "name": str(item["name"]),
            "samples": int(item["samples"]),
            "percent": float(item["percent"]),
            "evidence_ref": f"structured_evidence.top_functions[{index}]",
        }
        for index, item in enumerate(entries)
    ]


def _function_name_from_svg_title(title: str) -> str:
    first_line = title.splitlines()[0].strip()
    if "(" in first_line:
        first_line = first_line.split("(", 1)[0].strip()
    if " - " in first_line:
        first_line = first_line.split(" - ", 1)[0].strip()
    return first_line[:256]


def _percent_from_text(text: str) -> float:
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    return _safe_float(match.group(1)) if match else 0.0


def _samples_from_text(text: str) -> int:
    match = re.search(r"(\d[\d,]*)\s+(?:samples?|frames?)", text, flags=re.IGNORECASE)
    if not match:
        return 0
    return _safe_int(match.group(1).replace(",", ""))


def _build_stack_summary(
    depth: dict[str, Any],
    top_functions: list[dict[str, Any]],
    artifact_refs: list[dict[str, Any]],
    off_cpu_wait: Any = None,
) -> dict[str, Any]:
    stack_samples = depth.get("stack_samples", []) if isinstance(depth, dict) else []
    first_sample = stack_samples[0] if isinstance(stack_samples, list) and stack_samples and isinstance(stack_samples[0], dict) else {}
    first_top = top_functions[0] if top_functions else {}
    hot_frame = str(first_sample.get("hot_frame") or first_top.get("name") or "")
    call_path = str(first_sample.get("call_path") or "")
    sample_count = _safe_int(first_sample.get("sample_count") or first_top.get("samples"))
    percent = _safe_float(first_sample.get("percent") or first_top.get("percent"))
    off_cpu_summary = off_cpu_wait.get("summary") if isinstance(off_cpu_wait, dict) and isinstance(off_cpu_wait.get("summary"), dict) else {}
    off_cpu_stacks = off_cpu_wait.get("top_wait_stacks") if isinstance(off_cpu_wait, dict) and isinstance(off_cpu_wait.get("top_wait_stacks"), list) else []
    if off_cpu_stacks:
        first_wait = off_cpu_stacks[0] if isinstance(off_cpu_stacks[0], dict) else {}
        hot_frame = hot_frame or str(first_wait.get("top_frame") or "")
        sample_count = sample_count or _safe_int(off_cpu_summary.get("sample_count"))
        percent = percent or _safe_float(first_wait.get("percent"))
    parse_status = "ok" if hot_frame or stack_samples or top_functions else "insufficient_structured_signal"
    return {
        "dominant_hot_frame": hot_frame,
        "dominant_percent": round(percent, 2),
        "sample_count": sample_count,
        "stack_sample_count": (len(stack_samples) if isinstance(stack_samples, list) else 0) or len(off_cpu_stacks),
        "has_call_path": bool(call_path or depth.get("call_path") or (depth.get("context") or {}).get("call_path")),
        "has_wait_reason": bool(first_sample.get("wait_reason") or depth.get("wait_reason") or off_cpu_summary.get("has_wait_reason")),
        "top_wait_reason": str(off_cpu_summary.get("top_wait_reason") or first_sample.get("wait_reason") or depth.get("wait_reason") or ""),
        "total_wait_ms": round(_safe_float(off_cpu_summary.get("total_wait_ms")), 2),
        "parse_status": parse_status,
        "raw_payload_policy": "references_only",
        "artifact_ref_count": len(artifact_refs),
        "evidence_ref": "structured_evidence.stack_summary",
    }


def _build_call_path_hotspots(
    depth: dict[str, Any],
    top_functions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(depth, dict):
        return []
    context = depth.get("context", {}) if isinstance(depth.get("context"), dict) else {}
    stack_samples = depth.get("stack_samples", []) if isinstance(depth.get("stack_samples"), list) else []
    hotspots: list[dict[str, Any]] = []
    for index, item in enumerate(stack_samples[:HOTSPOT_LIMIT]):
        if not isinstance(item, dict):
            continue
        function = str(item.get("hot_frame") or (top_functions[0].get("name") if top_functions else "") or "")
        call_path_text = str(item.get("call_path") or context.get("call_path") or "")
        if not function and not call_path_text:
            continue
        hotspots.append({
            "function": function,
            "percent": round(_safe_float(item.get("percent")), 2),
            "samples": _safe_int(item.get("sample_count")),
            "call_path": [part for part in call_path_text.split(";") if part],
            "endpoint": str(item.get("endpoint") or context.get("endpoint") or ""),
            "service_id": str(item.get("service_id") or context.get("service_id") or context.get("service") or ""),
            "instance_id": str(item.get("instance_id") or context.get("instance_id") or context.get("instance") or ""),
            "context_id": str(item.get("context_id") or context.get("context_id") or ""),
            "evidence_ref": f"structured_evidence.call_path_hotspots[{index}]",
        })
    return hotspots


def _build_confidence_inputs(
    *,
    top_functions: list[dict[str, Any]],
    stack_summary: dict[str, Any],
    call_path_hotspots: list[dict[str, Any]],
    artifact_refs: list[dict[str, Any]],
    sys_metrics: Any,
    ebpf_metrics: Any,
    off_cpu_wait: Any,
    log_window: Any,
    dependency_check: Any,
    redis_check: Any,
    trace_profile: Any,
) -> dict[str, Any]:
    first_top = top_functions[0] if top_functions else {}
    context_completeness = "high" if call_path_hotspots else "medium" if stack_summary.get("has_call_path") else "low"
    artifact_types = [item["artifact_type"] for item in artifact_refs]
    return {
        "sample_count": _safe_int(stack_summary.get("sample_count") or first_top.get("samples")),
        "dominant_percent": round(_safe_float(stack_summary.get("dominant_percent") or first_top.get("percent")), 2),
        "context_completeness": context_completeness,
        "has_system_pressure": isinstance(sys_metrics, dict) and bool(sys_metrics),
        "has_wait_or_io_signal": _has_wait_or_io_signal(ebpf_metrics, off_cpu_wait),
        "has_log_signal": isinstance(log_window, dict) and bool(log_window.get("error_clusters")),
        "has_dependency_signal": isinstance(dependency_check, dict) and bool(dependency_check.get("checks")),
        "has_redis_signal": isinstance(redis_check, dict) and bool(redis_check),
        "failed_dependency_count": _failed_dependency_count(dependency_check),
        "log_error_cluster_count": _log_error_cluster_count(log_window),
        "redis_slowlog_entry_count": _redis_slowlog_entry_count(redis_check),
        "redis_max_latency_ms": _redis_max_latency_ms(redis_check),
        "parse_status": stack_summary.get("parse_status", "insufficient_structured_signal"),
        "artifact_types": artifact_types,
        "collector_families": _collector_families(artifact_types),
        "token_safety": "compact_summary_only",
        "trace_source_status": _trace_profile_status(trace_profile, "trace_source"),
        "stack_source_status": _trace_profile_status(trace_profile, "stack_source"),
        "trace_correlation_status": _trace_profile_status(trace_profile, "correlation_status"),
        "trace_max_supported_level": _trace_profile_status(trace_profile, "correlation_status", "max_supported_level"),
    }


def _build_evidence_index(
    *,
    artifact_refs: list[dict[str, Any]],
    depth: dict[str, Any],
    stack_summary: dict[str, Any],
    call_path_hotspots: list[dict[str, Any]],
    confidence_inputs: dict[str, Any],
    off_cpu_wait: Any,
    log_window: Any,
    dependency_check: Any,
    redis_check: Any,
    trace_profile: Any,
) -> dict[str, Any]:
    index = dict(depth) if isinstance(depth, dict) else {}
    index["artifact_refs"] = artifact_refs
    index["stack_summary"] = stack_summary
    index["call_path_hotspots"] = call_path_hotspots
    index["confidence_inputs"] = confidence_inputs
    if isinstance(off_cpu_wait, dict):
        index["off_cpu_wait"] = _compact_off_cpu_wait(off_cpu_wait)
    if isinstance(log_window, dict):
        index["log_scan"] = _compact_log_window(log_window)
    if isinstance(dependency_check, dict):
        index["dependency_check"] = _compact_dependency_check(dependency_check)
    if isinstance(redis_check, dict):
        index["redis_check"] = _compact_redis_check(redis_check)
    if isinstance(trace_profile, dict):
        index["trace_endpoint_profile"] = _compact_trace_profile(trace_profile)
    return index


def _trace_profile_hotspots(value: dict[str, Any]) -> list[dict[str, Any]]:
    hotspots = value.get("call_path_hotspots")
    if not isinstance(hotspots, list):
        return []
    result = []
    for index, item in enumerate(hotspots[:HOTSPOT_LIMIT]):
        if not isinstance(item, dict):
            continue
        function = str(item.get("function") or "")
        call_path = item.get("call_path") if isinstance(item.get("call_path"), list) else []
        if not function and not call_path:
            continue
        result.append({
            "function": function,
            "percent": round(_safe_float(item.get("percent")), 2),
            "samples": _safe_int(item.get("samples") or item.get("sample_count")),
            "call_path": [str(part) for part in call_path],
            "endpoint": str(item.get("endpoint") or ""),
            "service_id": str(item.get("service_id") or ""),
            "instance_id": str(item.get("instance_id") or ""),
            "context_id": str(item.get("context_id") or ""),
            "trace_ids": [str(item.get("trace_id"))] if item.get("trace_id") else list(item.get("trace_ids") or []),
            "correlation_method": str(item.get("correlation_method") or ""),
            "confidence": round(_safe_float(item.get("confidence")), 3),
            "evidence_ref": str(item.get("evidence_ref") or f"trace_endpoint_profile.call_path_hotspots[{index}]"),
        })
    return result


def _trace_profile_status(value: Any, section: str, field: str = "status") -> str:
    if not isinstance(value, dict):
        return ""
    section_value = value.get(section)
    if not isinstance(section_value, dict):
        return ""
    return str(section_value.get(field) or "")


def _compact_trace_profile(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "stack_source": value.get("stack_source", {}),
        "trace_source": {
            key: value.get("trace_source", {}).get(key)
            for key in ("kind", "status", "records_read", "records_in_window", "blocked_reason")
            if isinstance(value.get("trace_source"), dict) and key in value.get("trace_source", {})
        },
        "endpoint_bindings": (value.get("endpoint_bindings") or [])[:10],
        "call_path_hotspots": (value.get("call_path_hotspots") or [])[:10],
        "correlation_status": value.get("correlation_status", {}),
    }


def _collector_families(artifact_types: list[str]) -> list[str]:
    mapping = {
        "log_window_json": "log_scan",
        "dependency_check_json": "dependency_check",
        "redis_check_json": "redis_check",
        "top_json": "runtime_snapshot",
        "flamegraph_json": "runtime_snapshot",
        "depth_evidence_json": "runtime_snapshot",
        "continuous_top_json": "runtime_snapshot",
        "continuous_flamegraph_json": "runtime_snapshot",
        "off_cpu_wait_json": "off_cpu_wait_profile",
        "ebpf_metrics": "io_profile",
        "sys_metrics": "sys_metrics",
        "memory_json": "memory_profile",
        "trace_endpoint_profile_json": "trace_endpoint_profile",
    }
    return list(dict.fromkeys(mapping.get(item, item) for item in artifact_types))


def _compact_log_window(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": value.get("summary", {}),
        "error_clusters": (value.get("error_clusters") or [])[:5],
        "evidence_index": value.get("evidence_index", {}),
    }


def _compact_dependency_check(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": value.get("summary", {}),
        "checks": (value.get("checks") or [])[:10],
    }


def _compact_redis_check(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "connectivity": value.get("connectivity", {}),
        "info_summary": value.get("info_summary", {}),
        "slowlog_summary": value.get("slowlog_summary", {}),
        "latency_summary": value.get("latency_summary", {}),
    }


def _compact_off_cpu_wait(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": value.get("schema_version"),
        "collector_family": value.get("collector_family"),
        "summary": value.get("summary", {}),
        "event_summary": value.get("event_summary", {}),
        "cause_summary": value.get("cause_summary", {}),
        "stack_quality": value.get("stack_quality", {}),
        "top_wait_stacks": (value.get("top_wait_stacks") or [])[:5],
        "kernel_stacks": (value.get("kernel_stacks") or [])[:5],
        "thread_wait_summary": (value.get("thread_wait_summary") or [])[:10],
        "syscall_wait_summary": value.get("syscall_wait_summary", {}),
        "collector_status": value.get("collector_status"),
        "parser_status": value.get("parser_status"),
        "trace_source": value.get("trace_source", {}),
        "correlation": value.get("correlation", {}),
        "endpoint_bindings": (value.get("endpoint_bindings") or [])[:5],
        "call_path_hotspots": (value.get("call_path_hotspots") or [])[:5],
        "capability_check": value.get("capability_check", {}),
    }


def _has_wait_or_io_signal(ebpf_metrics: Any, off_cpu_wait: Any) -> bool:
    if isinstance(ebpf_metrics, dict) and bool(ebpf_metrics):
        return True
    if not isinstance(off_cpu_wait, dict):
        return False
    summary = off_cpu_wait.get("summary") if isinstance(off_cpu_wait.get("summary"), dict) else {}
    if _safe_int(summary.get("sample_count")) > 0:
        return True
    stacks = off_cpu_wait.get("top_wait_stacks")
    return isinstance(stacks, list) and bool(stacks)


def _failed_dependency_count(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    summary = value.get("summary")
    if isinstance(summary, dict) and isinstance(summary.get("failed_dependencies"), list):
        return len(summary["failed_dependencies"])
    checks = value.get("checks")
    if isinstance(checks, list):
        return sum(1 for item in checks if isinstance(item, dict) and item.get("success") is False)
    return 0


def _log_error_cluster_count(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    summary = value.get("summary")
    if isinstance(summary, dict):
        count = _safe_int(summary.get("error_cluster_count"))
        if count:
            return count
    clusters = value.get("error_clusters")
    return len(clusters) if isinstance(clusters, list) else 0


def _redis_slowlog_entry_count(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    slowlog = value.get("slowlog_summary")
    return _safe_int(slowlog.get("entry_count")) if isinstance(slowlog, dict) else 0


def _redis_max_latency_ms(value: Any) -> float:
    if not isinstance(value, dict):
        return 0.0
    latency = value.get("latency_summary")
    return _safe_float(latency.get("max_latency_ms")) if isinstance(latency, dict) else 0.0


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0
